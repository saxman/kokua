"""The ``capabilities`` toolset: the registry an agent can read, and a worker it can compose from it.

No registry is defined here. ``ToolsetRegistry`` already indexes every capability by name with a
description and a provider label, and reaches this module as ``ctx.state.registry``; what was missing
was a view of it a model can read and a way to act on what the view shows.

Discovery never calls ``Toolset.build``. Building has real side effects -- ``memory`` instantiates a
``SemanticMemoryStore`` and loads an embedding model, an MCP toolset touches live connections, a
plugin's build may fail -- and none of them should be paid to answer a question about what exists. That
is also why ``Toolset`` grows no field listing its tool names: a second declaration of what a toolset
contains could drift from what ``build`` returns, and the description is what the model picks by
anyway, exactly as it picks a skill from its catalogue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from aimu.tools import tool

from kokua.toolsets.registry import Setting, Toolset

if TYPE_CHECKING:
    from kokua.toolsets.context import ToolsetContext

DEFAULT_MAX_DEPTH = 3

#: The ``[capabilities]`` section of config.toml, owned here rather than by ``AssistantConfig``. Hot
#: because a runaway composition is something a user wants to rein in mid-session, without a restart.
CAPABILITIES_SETTINGS: tuple[Setting, ...] = (Setting("max_depth", int, DEFAULT_MAX_DEPTH, hot=True),)


def _catalogue(registry: Mapping[str, Toolset], filter_text: str) -> str:
    """One line per registry entry: name, provider, description, sorted by name.

    ``providers`` is read defensively because ``LiveState.registry`` is typed as a plain dict and a
    state built by hand may carry one; a missing provider map should degrade to an unlabeled line
    rather than break discovery.
    """
    providers = getattr(registry, "providers", {})
    needle = filter_text.strip().lower()
    lines = [
        f"{name} [{providers.get(name, 'unknown')}]: {registry[name].description}"
        for name in sorted(registry)
        if not needle or needle in name.lower() or needle in registry[name].description.lower()
    ]
    if not lines:
        return f"No capability matches {filter_text!r}. Call list_capabilities with no filter to see them all."
    return "\n".join(lines)


def make_capability_tools(ctx: "ToolsetContext") -> list:
    state = ctx.state

    @tool
    async def list_capabilities(filter: str = "") -> str:
        """List every capability installed on this machine, whether or not you currently hold it.

        Each line is a capability name, the kind of provider it came from, and what it does. Use this
        when a task needs something none of your own tools and none of your named sub-agent roles
        cover, then pass the names you need to compose_worker.

        Args:
            filter: Optional. Show only capabilities whose name or description contains this text.
        """
        return _catalogue(state.registry, filter)

    return [list_capabilities]


TOOLSET = Toolset(
    name="capabilities",
    description="Discover every installed capability and compose a sub-agent from the ones a task needs.",
    build=make_capability_tools,
    settings=CAPABILITIES_SETTINGS,
    # Discovering and composing is how an agent manages its own work rather than a domain capability,
    # so a lean supervisor declaring only it still reads as lean to the delegation guidance.
    cross_cutting=True,
)
