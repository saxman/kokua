"""Assembling the registry from every provider, and resolving one agent's declared names.

Provider order fixes which label a collision message blames first and is otherwise arbitrary: a
collision is an error, not a precedence rule, so nothing here depends on one provider winning.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Mapping

from kokua.config.file import ConfigError
from kokua.config.schema import AssistantConfig
from kokua.plugins import discover_toolsets
from kokua.toolsets.builtin import BUILTIN_TOOLSETS
from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.core import CORE_TOOLSETS
from kokua.toolsets.registry import Toolset, ToolsetError, build_tools, register, select

logger = logging.getLogger(__name__)


def _tolerate_build_failures(toolset: Toolset) -> Toolset:
    """Wrap a plugin's ``build`` so a failure logs a warning and yields no tools, instead of taking the
    assistant down.

    Applied only to plugin-sourced toolsets, deliberately: a core or AIMU toolset failing to build is a
    bug in this codebase and must be loud, so wrapping those too would hide the one class of failure this
    registry should never tolerate. Third-party code is the only thing the assistant should be tolerant
    of, since it is the only source whose failures this codebase cannot fix by editing itself.
    """
    build = toolset.build

    def _safe_build(ctx: ToolsetContext) -> list:
        try:
            return list(build(ctx))
        except Exception:
            logger.warning("Plugin toolset %r failed to build; skipping.", toolset.name, exc_info=True)
            return []

    return replace(toolset, build=_safe_build)


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
        sources.append(("plugin", [_tolerate_build_failures(t) for t in discover_toolsets().values()]))
    return register(sources)


def agent_tools(agent, names, state: LiveState, *, entry_point: str, agent_name: str) -> list:
    """The tool callables for one agent, resolved from its declared toolset names."""
    selected = select(names, state.registry, agent=agent_name, entry_point=entry_point)
    return build_tools(selected, ToolsetContext(state=state, agent=agent))


def validate_agents(config: AssistantConfig, registry: Mapping[str, Toolset]) -> None:
    """Reject a config whose agents cannot be built, before anything is built.

    Acyclicity is not a style rule. An agent's delegate is constructed by recursing into its targets'
    delegates, so a cycle would recurse until the stack is exhausted, at startup, with no useful
    message. Every check here fails loudly and names the offending value, because the previous
    per-role lists dropped unknown names silently and a typo produced a quietly smaller toolset.
    """
    if not config.agents:
        raise ConfigError(
            "no agents configured: config.toml needs at least one [agents.<name>] table. Run "
            "`kokua config init` to write a config with the default agents."
        )
    if config.entry_agent not in config.agents:
        known = ", ".join(sorted(config.agents))
        raise ConfigError(
            f"[assistant].agent names {config.entry_agent!r}, which has no [agents.{config.entry_agent}] "
            f"table. Configured agents: {known}."
        )
    for name, agent in config.agents.items():
        try:
            select(agent.tools, registry, agent=name, entry_point=config.entry_agent)
        except ToolsetError as e:
            raise ConfigError(str(e)) from e
        for target in agent.delegates_to:
            if target not in config.agents:
                known = ", ".join(sorted(config.agents))
                raise ConfigError(f"agent {name!r} delegates to unknown agent {target!r}. Configured agents: {known}.")
    _reject_cycles(config)


def _reject_cycles(config: AssistantConfig) -> None:
    """Depth-first search over ``delegates_to``, reporting the first cycle as the path that closes it."""
    path: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def walk(name: str) -> None:
        if name in on_path:
            cycle = " -> ".join(path[path.index(name) :] + [name])
            raise ConfigError(
                f"delegation cycle in [agents.*]: {cycle}. An agent's delegate is built by recursing "
                "into its targets, so the graph has to be acyclic."
            )
        if name in done:
            return
        path.append(name)
        on_path.add(name)
        for target in config.agents[name].delegates_to:
            walk(target)
        on_path.discard(name)
        path.pop()
        done.add(name)

    for name in config.agents:
        walk(name)
