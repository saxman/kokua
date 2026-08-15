"""Assembling the registry from every provider, and resolving one agent's declared names.

Provider order fixes which label a collision message blames first and is otherwise arbitrary: a
collision is an error, not a precedence rule, so nothing here depends on one provider winning.
"""

from __future__ import annotations

from kokua.config.schema import AssistantConfig
from kokua.plugins import discover_toolsets
from kokua.toolsets.builtin import BUILTIN_TOOLSETS
from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.core import CORE_TOOLSETS
from kokua.toolsets.registry import Toolset, build_tools, register, select


def build_registry(config: AssistantConfig) -> dict[str, Toolset]:
    """Every toolset an agent may name, by name.

    Plugin discovery is gated on ``config.load_plugins``, which is a "do not execute third-party code"
    switch rather than a naming switch: with it off, a config naming a plugin toolset fails at startup
    with the unknown-name error rather than starting an agent quietly missing a capability.
    """
    sources: list[tuple[str, list[Toolset]]] = [
        ("AIMU capability", list(BUILTIN_TOOLSETS)),
        ("core subsystem", list(CORE_TOOLSETS)),
    ]
    if config.load_plugins:
        sources.append(("plugin", list(discover_toolsets().values())))
    return register(sources)


def agent_tools(agent, names, state: LiveState, *, entry_point: str, agent_name: str) -> list:
    """The tool callables for one agent, resolved from its declared toolset names."""
    selected = select(names, state.registry, agent=agent_name, entry_point=entry_point)
    return build_tools(selected, ToolsetContext(state=state, agent=agent))
