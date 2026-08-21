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
from importlib.resources import files
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from kokua import images
from kokua.core.assistant import Assistant, ModelClientError
from kokua.channels.web import WebChannel
from kokua.config import AssistantConfig, ConfigError
from kokua.plugins import FrontEnd
from kokua.toolsets.agents import validated_registry

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


_CONTROL_TYPES = ("new", "select", "delete", "settings", "get_settings", "get_tasks", "task")


def _parse_control(raw: str) -> Optional[dict]:
    """Return a control object ({"type": "new"/"select"/"delete"/"settings"/"task"/..., ...}), else None.

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


def _parse_image_input(raw: str) -> Optional[tuple[str, list[str]]]:
    """Return ``(text, image_data_urls)`` for an ``{"type": "input", ...}`` frame carrying images, else None.

    An input frame without images returns None so it falls through to the ordinary text path (the page
    only sends this frame shape when at least one image is attached)."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not (isinstance(obj, dict) and obj.get("type") == "input"):
        return None
    images_field = obj.get("images")
    if not isinstance(images_field, list) or not images_field:
        return None
    urls = [u for u in images_field if isinstance(u, str)]
    return str(obj.get("text", "")), urls


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
    if assistant.turn_running(assistant.active_id):
        await channel.send_working(True)


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
        channel = WebChannel(websocket, show_thinking=config.show_thinking, show_tools=config.show_tools)
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
            # Conversation controls (new/select/delete) are handled here and never reach the channel; an
            # "input" frame carrying attached images is decoded to on-disk files and fed with its text; all
            # other frames (chat, "/stop", approval "y"/"n") are fed to the channel as today. On
            # disconnect, the sentinel ends receive(), stopping the scheduler and assistant.run().
            try:
                while True:
                    raw = await websocket.receive_text()
                    image_input = _parse_image_input(raw)
                    if image_input is not None:
                        text, data_urls = image_input
                        # Save each upload to disk, then hand the agent the filesystem paths (AIMU
                        # base64-inlines them for the model; persistence later compacts them back to the
                        # same /images/<hash> reference). Undecodable data URLs are dropped.
                        paths = []
                        for data_url in data_urls:
                            reference = images.save_data_url(config.images_path, data_url)
                            if reference:
                                paths.append(str(images.reference_to_path(config.images_path, reference)))
                        await channel.feed_input(text, paths)
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
            except WebSocketDisconnect:
                pass
            finally:
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
