"""The web front end: a Starlette + uvicorn WebSocket server hosting the assistant.

Serves a static chat page and bridges one browser onto a per-connection Assistant session via
`WebChannel`. Async-native, so scheduler-pushed proactive messages reach the browser unprompted.
Requires the ``web`` extra (``pip install 'kokua[web]'``). Single user by design: one session per
connection, sharing one history / skills / memory; a second simultaneous connection is rejected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from kokua import images
from kokua.core.assistant import Assistant, ModelClientError
from kokua.core.conversations import ID_PREFIX_MIN
from kokua.core.messages import derive_title
from kokua.channels.web import WebChannel
from kokua.config import AssistantConfig, ConfigError
from kokua.plugins import FrontEnd
from kokua.core.agents import validated_registry
from kokua.transcript_export import DEFAULT_MAX_PAYLOAD_CHARS, render_markdown

logger = logging.getLogger(__name__)


def _index_html() -> str:
    """Read the bundled chat page from package data (works for installed + source layouts)."""
    return files("kokua").joinpath("web_static/index.html").read_text(encoding="utf-8")


# Browser assets served at the page's root (the page loads them by relative URL). Text assets map
# filename -> media type; the KaTeX fonts (binary woff2) are served from the /fonts/ subpath.
# app.css/app.js are the page's own stylesheet and script, split out of index.html so each file holds
# one language; the rest are vendored libraries.
_STATIC_ASSETS = {
    "app.css": "text/css",
    "app.js": "text/javascript",
    "marked.min.js": "text/javascript",
    "purify.min.js": "text/javascript",
    "katex.min.js": "text/javascript",
    "auto-render.min.js": "text/javascript",
    "katex.min.css": "text/css",
}


def _static_text(filename: str) -> str:
    return files("kokua").joinpath(f"web_static/{filename}").read_text(encoding="utf-8")


_CONTROL_TYPES = ("new", "select", "delete", "settings", "get_settings", "get_tasks", "task", "export")


def _parse_control(raw: str) -> Optional[dict]:
    """Return a control object ({"type": "new"/"select"/"delete"/"settings"/"task"/"export"/..., ...}),
    else None.

    Anything that is not exactly such a JSON object is a normal channel message (chat, "/stop",
    approval "y"/"n") and is fed to the channel unchanged.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and obj.get("type") in _CONTROL_TYPES:
        return obj
    return None


def _parse_input(raw: str) -> Optional[tuple[str, list[str], Optional[str]]]:
    """Return ``(text, image_data_urls, thinking)`` for an ``{"type": "input", ...}`` frame, else None.

    The page sends this shape whenever a message carries anything beyond its own text: attached images,
    a per-turn reasoning effort, or both. A frame carrying neither still parses, into an empty list and a
    None, because declining it here would drop its text back to the plain-string path, which would then
    feed the raw JSON to the model as if the user had typed it.

    A non-string ``thinking`` is dropped rather than passed on. The core normalizes the value anyway, so
    this is not the check that makes it safe; it is what keeps a malformed frame from travelling.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not (isinstance(obj, dict) and obj.get("type") == "input"):
        return None
    images_field = obj.get("images")
    urls = [u for u in images_field if isinstance(u, str)] if isinstance(images_field, list) else []
    thinking = obj.get("thinking")
    return str(obj.get("text", "")), urls, thinking if isinstance(thinking, str) else None


async def _sync_view(channel: WebChannel, assistant: Assistant) -> None:
    """Point the channel at the conversation being viewed and refresh the sidebar/history for it.

    Sets ``channel.active_conversation_id`` before pushing conversations/history, so a turn on this
    conversation streams from now on. The assistant keeps this in sync on every later select/new/delete
    (``Assistant._sync_channel_active_id``); this call covers the connect-time case that precedes any of
    those (otherwise a fresh connection's channel would default to ``None``), and calling it again after
    an op is a harmless re-assignment of the value the assistant already set.

    If the conversation being switched into already has an in-flight turn (started before this switch,
    still running in the background), tell the page so it shows a "working" indicator instead of looking
    idle until that turn's next frame arrives.
    """
    channel.active_conversation_id = assistant.active_id
    await channel.send_conversations(assistant.list_conversations())
    await channel.send_history(assistant.history, assistant.history_metadata)
    elapsed = assistant.turn_elapsed(assistant.active_id)
    if elapsed is not None:
        await channel.send_working(elapsed)


# How many words of a conversation's title become its exported filename's slug. Enough to tell two
# exports of the same day apart at a glance in a downloads folder; the id fragment appended after it
# is what actually guarantees uniqueness.
_EXPORT_SLUG_WORDS = 6


def _export_filename(session_key: str, title: str) -> str:
    """A bare filename (no directory component) for one conversation's Markdown export.

    Built from today's date, a slug of the title, and a short leading fragment of the conversation's
    id: readable in a downloads folder and, unlike the title itself, always safe to hand to the
    ``/download/{name}`` route. The slug keeps only ``[a-z0-9]`` runs from the title and joins them
    with hyphens, which is an allowlist rather than an escape: a title holding a "/" or a ".."
    segment (either of which the route's ``name != Path(name).name`` check rejects) contributes
    nothing to the slug rather than surviving in some encoded form, so the result can never fail that
    check no matter what the conversation was named.
    """
    words = re.findall(r"[a-z0-9]+", title.lower())[:_EXPORT_SLUG_WORDS]
    slug = "-".join(words) or "conversation"
    date = datetime.now().strftime("%Y-%m-%d")
    return f"{date}-{slug}-{session_key[:ID_PREFIX_MIN]}.md"


async def _export_conversation(
    channel: WebChannel, assistant: Assistant, config: AssistantConfig, conversation_id: str
) -> None:
    """Render one conversation to Markdown, write it under ``downloads_path``, and point the page at it.

    Resolves through ``assistant.resolve_conversation`` rather than ``select_conversation``: an export
    reads a conversation, it does not switch the view onto it, so the sidebar's active row and the
    displayed history must stay exactly as they were. Raises when the id names none (or more than
    one); the caller in ``serve_connection`` turns that into a spoken apology rather than a closed
    socket, the same "answer, don't drop the connection" contract every other control in that loop
    keeps.
    """
    session = assistant.resolve_conversation(conversation_id)
    if session is None:
        raise ValueError(f"no conversation found matching {conversation_id!r} to export")
    markdown = render_markdown(session, max_payload_chars=DEFAULT_MAX_PAYLOAD_CHARS)
    title = session.metadata.get("title") or derive_title(session.messages) or ""
    name = _export_filename(session.key, title)
    # The download route serves out of this directory and expects it to already exist (it 404s
    # rather than creating anything); a fresh $KOKUA_HOME may never have had a file written into it.
    config.downloads_path.mkdir(parents=True, exist_ok=True)
    (config.downloads_path / name).write_text(markdown, encoding="utf-8")
    await channel.send_download(name, f"/download/{name}")


def build_app(config: AssistantConfig, *, client=None, client_factory=None) -> Starlette:
    """Build the Starlette app serving the chat page (``/``) and the WebSocket (``/ws``).

    ``client`` injects a single model client (single-conversation tests pass a mock);
    ``client_factory`` injects a per-conversation client factory (multi-conversation tests);
    production leaves both None so each conversation builds its own via ``Assistant.create``.

    Raises ``ConfigError`` if the agents in ``config.toml`` cannot resolve. This front end builds its
    assistant per connection, so without a check here the only report of a broken ``[agents.*]`` table
    would be a WebSocket that closes: the server would come up, serve the page, and refuse every
    connection it made. Failing while the app is built puts the message on the terminal, which is where
    the CLI front end already reports the same mistake. The registry is discarded because each
    connection's ``Assistant.create`` builds its own; this call is for its error.
    """
    validated_registry(config)

    busy = {"active": False}  # one-active-connection guard (single user, single process)

    async def index(request):
        return HTMLResponse(_index_html())

    async def static_asset(request):
        name = request.path_params["name"]
        media = _STATIC_ASSETS.get(name)  # allowlist -> only the known vendored files
        if media is None:
            return Response(status_code=404)
        return Response(_static_text(name), media_type=media)

    async def static_font(request):
        # Serve a vendored KaTeX woff2 font referenced by katex.min.css (url(fonts/KaTeX_*.woff2)).
        # The name must be a bare KaTeX woff2 filename; the allowlist pattern blocks any traversal.
        name = request.path_params["name"]
        if name != Path(name).name or not (name.startswith("KaTeX_") and name.endswith(".woff2")):
            return Response(status_code=404)
        resource = files("kokua").joinpath(f"web_static/fonts/{name}")
        if not resource.is_file():
            return Response(status_code=404)
        return Response(resource.read_bytes(), media_type="font/woff2")

    async def download(request):
        # Serve a file from the downloads folder (e.g. a PDF from the markdown_to_pdf tool). The
        # {name:str} route converter already excludes "/"; the basename check and is_file() guard
        # against any remaining traversal, and nothing outside downloads_path is reachable.
        name = request.path_params["name"]
        if name != Path(name).name:
            return Response(status_code=404)
        path = config.downloads_path / name
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, filename=name)

    async def image(request):
        # Serve an uploaded or generated image from the images folder. Same traversal guard as
        # download; nothing outside images_path is reachable. Referenced by the page as /images/<name>.
        name = request.path_params["name"]
        if name != Path(name).name:
            return Response(status_code=404)
        path = config.images_path / name
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path)

    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        if busy["active"]:
            await websocket.send_json(
                {"type": "message", "text": "Assistant is busy in another tab.", "proactive": False}
            )
            await websocket.close()
            return
        busy["active"] = True
        channel = WebChannel(websocket)
        try:
            await serve_connection(websocket, channel)
        finally:
            # Released on every exit, not only the serve loop's. The guard is taken before the assistant
            # exists, so a failure in between leaks it and refuses every later connection as "busy in
            # another tab" -- a confident wrong diagnosis of a fault no other tab had anything to do with.
            busy["active"] = False
            await channel.aclose()

    async def serve_connection(websocket: WebSocket, channel: WebChannel) -> None:
        try:
            assistant = await Assistant.create(config, channel, client=client, client_factory=client_factory)
        except (ModelClientError, ConfigError) as e:
            # Either the model client could not be built (no model resolved, or a bad model string) or
            # config.toml no longer resolves (an [agents.*] table naming a toolset since renamed, or moved
            # out to a skill). Both are user mistakes whose message states the fix, so it goes to the
            # browser: closing the socket on its own leaves the page able to say only "Disconnected."
            await channel.send(str(e))
            await websocket.close()
            return
        # Show the conversation list, the active conversation's history, the current settings, and the
        # scheduled tasks on (re)connect, so a client's sidebar, chat, and settings view are all populated.
        await _sync_view(channel, assistant)
        await channel.send_settings(assistant.current_settings())
        # Unguarded, unlike the two `list_tasks` calls in the loop below: `Assistant.create` just armed
        # every task from the same file, so a config this could not parse would already have left above.
        await channel.send_tasks(assistant.list_tasks())

        async def pump() -> None:
            """Read the socket in this task and apply what arrives in another, in arrival order.

            The two halves are split because they fail differently. Applying a frame can take arbitrarily
            long: a control that waits on the turn gate waits for in-flight turns to drain, which on a
            local model is minutes. Reading has to keep happening anyway, because a disconnect *is* a
            read, and this is the only task positioned to notice one. Handled inline (as this loop used to
            be), a slow control stopped the reading too: the disconnect behind it was never seen, so the
            one-connection guard in ``ws_endpoint`` was never released and the reload that would have
            recovered the page was refused as "busy in another tab". Killing the process was the only way
            out of a UI that had merely been asked to wait.

            One queue and one applying task, rather than a task per frame, because order is part of the
            contract: selecting a conversation and then sending a message has to bind the message to the
            conversation just selected. So a slow control still delays the frames behind it, including a
            ``/stop``. That is the honest cost of ordering, and it is now a delay rather than a wedge:
            reloading always works, and the reload tears the blocked handler down with the connection.
            """
            inbound: asyncio.Queue = asyncio.Queue()  # unbounded: a reader that blocks is the bug above

            # Conversation controls (new/select/delete) are handled here and never reach the channel; an
            # "input" frame carrying attached images or a per-turn reasoning effort is decoded and fed with
            # its text; all other frames (chat, "/stop", approval "y"/"n") are fed to the channel as today.
            async def apply_frames() -> None:
                while True:
                    raw = await inbound.get()
                    parsed = _parse_input(raw)
                    if parsed is not None:
                        text, data_urls, thinking = parsed
                        # Save each upload to disk, then hand the agent the filesystem paths (AIMU
                        # base64-inlines them for the model; persistence later compacts them back to the
                        # same /images/<hash> reference). Undecodable data URLs are dropped.
                        paths = []
                        for data_url in data_urls:
                            reference = images.save_data_url(config.images_path, data_url)
                            if reference:
                                paths.append(str(images.reference_to_path(config.images_path, reference)))
                        await channel.feed_input(text, paths, thinking=thinking)
                        continue
                    control = _parse_control(raw)
                    if control is None:
                        await channel.feed(raw)
                        continue
                    # Settings controls only touch model config, not the conversation list, so they
                    # return the current settings and skip the sidebar/history refresh below.
                    if control["type"] == "get_settings":
                        await channel.send_settings(assistant.current_settings())
                        continue
                    if control["type"] == "settings":
                        try:
                            await assistant.apply_settings(control.get("values", {}))
                        except Exception:
                            logger.warning("Could not apply settings", exc_info=True)
                            await channel.send("Sorry, those settings could not be applied.")
                        await channel.send_settings(assistant.current_settings())
                        continue
                    # An export reads a conversation rather than switching the view onto it, so
                    # (unlike select/new/delete) it too skips the sidebar/history refresh below: it
                    # changes nothing about what the page is showing.
                    if control["type"] == "export":
                        try:
                            await _export_conversation(channel, assistant, config, str(control.get("id", "")))
                        except Exception:
                            logger.warning("Could not export conversation", exc_info=True)
                            await channel.send("Sorry, that conversation could not be exported.")
                        continue
                    # Task controls mostly touch only the scheduled-task registry, so like the settings
                    # controls they answer with a fresh task list and skip the history refresh.
                    if control["type"] == "get_tasks":
                        try:
                            # list_tasks raises rather than reading an empty list from a config a
                            # mid-session hand-edit left unparseable, so that failure never reads as
                            # "there are no tasks" and looks like every scheduled task was cancelled.
                            await channel.send_tasks(assistant.list_tasks())
                        except Exception:
                            logger.warning("Could not list tasks", exc_info=True)
                            await channel.send("Sorry, the task list could not be loaded.")
                        continue
                    if control["type"] == "task":
                        action = str(control.get("action", ""))
                        try:
                            # task_action allowlists the action, so an unrecognized one raises here
                            # rather than reaching the registry.
                            assistant.task_action(action, str(control.get("name", "")))
                        except Exception:
                            logger.warning("Could not apply task action", exc_info=True)
                            await channel.send("Sorry, that task action could not be applied.")
                        # "stop" is the one action whose effect is not in the task list at all: the page
                        # reads whether a task is running off the running marker on its conversations, so
                        # the button it just used would otherwise linger until the run's own push landed.
                        if action == "stop":
                            await channel.send_conversations(assistant.list_conversations())
                        try:
                            await channel.send_tasks(assistant.list_tasks())
                        except Exception:
                            logger.warning("Could not list tasks", exc_info=True)
                            await channel.send("Sorry, the task list could not be loaded.")
                        continue
                    if control["type"] == "new":
                        try:
                            await assistant.new_conversation()
                        except ModelClientError:
                            logger.warning("Could not build agent for new conversation", exc_info=True)
                            await channel.send("Sorry, that conversation could not be created.")
                            continue
                    elif control["type"] == "select":
                        try:
                            await assistant.select_conversation(control["id"])
                        except ModelClientError:
                            logger.warning("Could not build agent for conversation switch", exc_info=True)
                            await channel.send("Sorry, that conversation could not be opened.")
                            continue
                    elif control["type"] == "delete":
                        try:
                            await assistant.delete_conversation(control["id"])
                        except ModelClientError:
                            logger.warning("Could not build agent after conversation delete", exc_info=True)
                            await channel.send("Sorry, that conversation could not be deleted.")
                            continue
                    await _sync_view(channel, assistant)

            async def read_socket() -> None:
                try:
                    while True:
                        inbound.put_nowait(await websocket.receive_text())
                except WebSocketDisconnect:
                    pass

            # Whichever half finishes first ends the connection, in both directions. A disconnect ends
            # the reader, which is the ordinary case. An unexpected error in a control ends the applier,
            # and has to end the reader with it: left running, it would queue frames into a drain nobody
            # reads, which is this same wedge in a new place. `result()` re-raises that error rather than
            # letting `gather` below swallow it, so an unhandled failure still tears the connection down
            # the way it did when controls were applied inline.
            applying = asyncio.create_task(apply_frames())
            reading = asyncio.create_task(read_socket())
            try:
                done, _ = await asyncio.wait({applying, reading}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                applying.cancel()
                reading.cancel()
                await asyncio.gather(applying, reading, return_exceptions=True)
                # The sentinel ends receive(), which stops the scheduler and assistant.run().
                await channel.feed(None)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(pump())
            tg.create_task(assistant.run())

    return Starlette(
        routes=[
            Route("/", index),
            Route("/download/{name:str}", download),  # generated files (e.g. markdown_to_pdf PDFs)
            Route("/images/{name:str}", image),  # uploaded + generated images
            Route("/fonts/{name:str}", static_font),  # vendored KaTeX woff2 fonts
            Route("/{name:str}", static_asset),  # vendored marked / purify / katex js + css
            WebSocketRoute("/ws", ws_endpoint),
        ]
    )


async def run(config: AssistantConfig, args: argparse.Namespace) -> None:
    """Run the web server within the current asyncio loop (for the unified `kokua --frontend web`)."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(build_app(config), host=config.host, port=config.port))
    await server.serve()


def serve(config: AssistantConfig, **uvicorn_kwargs) -> None:
    """Blocking server start, used by the `kokua-web` console script."""
    import uvicorn

    uvicorn.run(build_app(config), host=config.host, port=config.port, **uvicorn_kwargs)


def main() -> None:
    # Resolve config (defaults < file < flags), then serve. The `kokua-web` console script reaches
    # this through `cli.main_web`, which runs the AIMU preflight first: importing *this* module
    # already pulls in the AIMU surface that preflight exists to check, so the check cannot live here.
    from ..cli import build_arg_parser, resolve_config

    args = build_arg_parser("kokua-web").parse_args()
    serve(resolve_config(args))


FRONTEND = FrontEnd(
    name="web",
    description="Serve a browser chat UI over WebSocket (needs the 'web' extra).",
    run=run,
)


if __name__ == "__main__":
    main()
