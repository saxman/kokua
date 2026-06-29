"""The plugin system: front ends and tool-packs discovered via Python entry points.

Mopai is modular. A **front end** runs the assistant over some transport (terminal, web, and
later Telegram/Slack); a **tool-pack** contributes extra agent tools. Both are discovered at
runtime from entry-point groups, so a third party adds one by publishing a package that
registers an entry point, with no change to Mopai's core:

    [project.entry-points."mopai.frontends"]
    telegram = "mopai_telegram:FRONTEND"

    [project.entry-points."mopai.tools"]
    weather = "my_weather_pack:TOOL_PACK"

The built-in `cli` / `web` front ends and the `example` tool-pack are registered exactly this
way in Mopai's own pyproject. A hardcoded fallback registry also lists the built-ins so the app
still works when run from a source checkout that hasn't been ``pip install``-ed (no entry-point
metadata yet).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Awaitable, Callable

FRONTEND_GROUP = "mopai.frontends"
TOOL_PACK_GROUP = "mopai.tools"


@dataclass(frozen=True)
class FrontEnd:
    """A way to run the assistant over a transport.

    ``run`` receives the resolved config and the parsed CLI args and drives the assistant to
    completion (it is responsible for building the channel(s) and the server lifecycle, if any).
    """

    name: str
    description: str
    run: Callable[["object", argparse.Namespace], Awaitable[None]]


@dataclass(frozen=True)
class ToolPack:
    """A bundle of extra agent tools.

    ``build`` receives the assistant config and returns a list of ``@aimu.tool`` callables to add
    to the agent (each must carry ``__name__`` / ``__tool_spec__``). Names should be distinct from
    the built-in tools to avoid shadowing.
    """

    name: str
    description: str
    build: Callable[["object"], list]


def _builtin_frontends() -> dict[str, FrontEnd]:
    # Imported lazily so importing this module doesn't pull the web stack (starlette/uvicorn).
    from .frontends import cli as cli_frontend
    from .frontends import web as web_frontend

    return {cli_frontend.FRONTEND.name: cli_frontend.FRONTEND, web_frontend.FRONTEND.name: web_frontend.FRONTEND}


def _load_group(group: str) -> dict[str, object]:
    loaded: dict[str, object] = {}
    for ep in entry_points(group=group):
        try:
            loaded[ep.name] = ep.load()
        except Exception:  # a broken third-party plugin must not take down discovery
            continue
    return loaded


def discover_frontends() -> dict[str, FrontEnd]:
    """Return all available front ends by name (entry points, with built-ins as a fallback)."""
    found = dict(_builtin_frontends())
    found.update({name: obj for name, obj in _load_group(FRONTEND_GROUP).items() if isinstance(obj, FrontEnd)})
    return found


def get_frontend(name: str) -> FrontEnd:
    """Resolve a front end by name, raising a clear error listing the choices on a miss."""
    found = discover_frontends()
    try:
        return found[name]
    except KeyError:
        choices = ", ".join(sorted(found)) or "(none)"
        raise KeyError(f"unknown front end {name!r}; available: {choices}") from None


def discover_tool_packs() -> dict[str, ToolPack]:
    """Return all installed tool-packs by name (from the ``mopai.tools`` entry-point group)."""
    return {name: obj for name, obj in _load_group(TOOL_PACK_GROUP).items() if isinstance(obj, ToolPack)}
