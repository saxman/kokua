"""The plugin system: front ends and toolsets discovered via Python entry points.

Kokua is modular. A **front end** runs the assistant over some transport (terminal, web, and
later Telegram/Slack); a **toolset** contributes extra agent tools. Both are discovered at
runtime from entry-point groups, so a third party adds one by publishing a package that
registers an entry point, with no change to Kokua's core:

    [project.entry-points."kokua.frontends"]
    telegram = "kokua_telegram:FRONTEND"

    [project.entry-points."kokua.toolsets"]
    weather = "my_weather_pack:TOOLSET"

The built-in `cli` / `web` front ends and the `example` toolset are registered exactly this
way in Kokua's own pyproject. A hardcoded fallback registry also lists the built-in front ends
so the app still works when run from a source checkout that hasn't been ``pip install``-ed (no
entry-point metadata yet).

``Toolset`` and ``ToolsetContext`` are re-exported here from ``kokua.toolsets``, which is where
they are defined: this module is the public surface a third party imports from, while Kokua's
own code imports them directly from ``kokua.toolsets``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Awaitable, Callable

from kokua.toolsets import Toolset, ToolsetContext

FRONTEND_GROUP = "kokua.frontends"
TOOLSET_GROUP = "kokua.toolsets"

# This project's own distribution name (see kokua/__init__.py's version("kokua")). Kokua's five built-in
# toolsets (example, aimu_agents, pdf, image, email) register under TOOLSET_GROUP exactly as a
# third-party plugin would; own_distribution_toolset_names tells those apart from a real third party's by
# checking which distribution registered the entry point, so a sixth one added later needs no update here.
_OWN_DISTRIBUTION = "kokua"

__all__ = [
    "FRONTEND_GROUP",
    "TOOLSET_GROUP",
    "FrontEnd",
    "Toolset",
    "ToolsetContext",
    "discover_frontends",
    "discover_toolsets",
    "get_frontend",
    "own_distribution_toolset_names",
]


@dataclass(frozen=True)
class FrontEnd:
    """A way to run the assistant over a transport.

    ``run`` receives the resolved config and the parsed CLI args and drives the assistant to
    completion (it is responsible for building the channel(s) and the server lifecycle, if any).
    """

    name: str
    description: str
    run: Callable[["object", argparse.Namespace], Awaitable[None]]


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


def discover_toolsets() -> dict[str, Toolset]:
    """Every installed toolset by name, from the ``kokua.toolsets`` entry-point group."""
    return {name: obj for name, obj in _load_group(TOOLSET_GROUP).items() if isinstance(obj, Toolset)}


def own_distribution_toolset_names() -> set[str]:
    """Names in the ``kokua.toolsets`` group that Kokua's own distribution registered, not a third party's.

    A name here ships in the box regardless of what any config declares, so an agent not naming it is not
    news the way an unreferenced third-party plugin or a configured MCP server would be -- see
    ``toolsets.agents.unreferenced_toolsets``. Queried independently of ``discover_toolsets`` (a second
    ``entry_points`` call) rather than folded into it, since provenance is a distinct question from "what
    is installed" and callers of ``discover_toolsets`` should not have to care about it.
    """
    return {
        ep.name for ep in entry_points(group=TOOLSET_GROUP) if ep.dist is not None and ep.dist.name == _OWN_DISTRIBUTION
    }
