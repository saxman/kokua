"""The web front end: a Starlette + uvicorn WebSocket server hosting the assistant.

Serves a static chat page and bridges one browser onto a per-connection Assistant session via
`WebChannel`. Async-native, so scheduler-pushed proactive messages reach the browser unprompted.
Requires the ``web`` extra (``pip install 'kokua[web]'``). Single user by design: one session per
connection, sharing one history / skills / memory; a second simultaneous connection is rejected.
"""

from __future__ import annotations

import argparse
import asyncio
from importlib.resources import files

from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..assistant import Assistant
from ..channels.web import WebChannel
from ..config import AssistantConfig
from ..plugins import FrontEnd


def _index_html() -> str:
    """Read the bundled chat page from package data (works for installed + source layouts)."""
    return files("kokua").joinpath("web_static/index.html").read_text(encoding="utf-8")


def build_app(config: AssistantConfig, *, client=None) -> Starlette:
    """Build the Starlette app serving the chat page (``/``) and the WebSocket (``/ws``).

    ``client`` injects a model client (tests pass a mock); production leaves it None so each
    connection builds its own via ``Assistant.create``.
    """
    busy = {"active": False}  # one-active-connection guard (single user, single process)

    async def index(request):
        return HTMLResponse(_index_html())

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
        assistant = await Assistant.create(config, channel, client=client)

        async def pump() -> None:
            # Feed inbound frames to the channel; on disconnect, the sentinel ends receive(),
            # which stops the scheduler and lets assistant.run() (and this group) return.
            try:
                while True:
                    await channel.feed(await websocket.receive_text())
            except WebSocketDisconnect:
                pass
            finally:
                await channel.feed(None)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(pump())
                tg.create_task(assistant.run())
        finally:
            busy["active"] = False
            await channel.aclose()

    return Starlette(routes=[Route("/", index), WebSocketRoute("/ws", ws_endpoint)])


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
    # The `kokua-web` convenience script: resolve config (defaults < file < flags), then serve.
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
