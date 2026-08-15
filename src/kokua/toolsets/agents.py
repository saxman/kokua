"""Assembling the registry from every provider, and resolving one agent's declared names.

Provider order fixes which label a collision message blames first and is otherwise arbitrary: a
collision is an error, not a precedence rule, so nothing here depends on one provider winning.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Callable, Mapping, Optional, Sequence

from aimu.aio.tools.builtin import make_async_subagent_tool

from kokua.config.file import ConfigError
from kokua.config.schema import DEFAULT_SYSTEM_MESSAGE, AssistantConfig
from kokua.plugins import discover_toolsets
from kokua.toolsets.builtin import BUILTIN_TOOLSETS
from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.core import CORE_TOOLSETS
from kokua.toolsets.registry import Toolset, ToolsetError, ToolsetRegistry, build_tools, register, select

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


# Provider labels `build_registry` hands to `register`. Named once here so `unreferenced_toolsets` can
# tell a name nobody provisioned (a built-in AIMU group, a core subsystem) from a name the user actually
# provisioned (a plugin, and eventually a configured MCP server) without duplicating the strings.
_AIMU_PROVIDER = "AIMU capability"
_CORE_PROVIDER = "core subsystem"
_PLUGIN_PROVIDER = "plugin"

# Providers whose toolsets ship regardless of what any agent declares, so a name from one of these being
# unreferenced is not news -- unlike a plugin the user installed, or a server the user configured, which
# earn a spot in the [agents.*] tables specifically so they can be reached.
_UNPROVISIONED_PROVIDERS = {_AIMU_PROVIDER, _CORE_PROVIDER}


def build_registry(config: AssistantConfig) -> ToolsetRegistry:
    """Every toolset an agent may name, by name.

    Plugin discovery is gated on ``config.load_plugins``, which is a "do not execute third-party code"
    switch rather than a naming switch: with it off, a config naming a plugin toolset fails at startup
    with the unknown-name error rather than starting an agent quietly missing a capability.
    """
    sources: list[tuple[str, list[Toolset]]] = [
        (_AIMU_PROVIDER, list(BUILTIN_TOOLSETS)),
        (_CORE_PROVIDER, list(CORE_TOOLSETS)),
    ]
    if config.load_plugins:
        sources.append((_PLUGIN_PROVIDER, [_tolerate_build_failures(t) for t in discover_toolsets().values()]))
    return register(sources)


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


# The delegation mechanism, given to any agent with a non-empty delegates_to. "Answer trivial or
# conversational requests directly with your own tools" is unconditional -- true of any delegating agent
# regardless of what it holds, and without it the model over-delegates, spawning a worker to answer a
# greeting. It deliberately does not enumerate which tools those are: that enumeration would be a
# hand-maintained copy of the agent's declared toolset, stale the moment config.toml changes, and
# redundant besides -- the model already sees its actual tools in the tool schema, and each toolset's own
# guidance already says what it is for. The worker menu itself is AIMU's: it renders the agent_types into
# the spawn tool's docstring.
DELEGATION_GUIDANCE = (
    " Answer trivial or conversational requests directly with your own tools. Delegate specialized work "
    "by calling `spawn_subagent(agent_type, task)`: pick the worker whose role fits, give it a complete, "
    "self-contained task (it shares no history with you), then relay or synthesize its answer for the "
    "user. Emit several `spawn_subagent` calls when subtasks are independent."
)

# Added only when every toolset the agent declared is cross_cutting. Without the "almost no direct
# tools" clause a lean agent answers web, file, and code questions from memory instead of spawning a
# worker that has the tools; without the "lean supervisor" framing preceding it, the sentence has no
# subject. Both halves are derived from the declaration rather than asserted unconditionally: an agent
# holding a domain toolset is neither a lean supervisor nor genuinely short on direct tools, so granting
# it one removes both claims instead of leaving the prompt contradicting the advertised tools.
LEAN_DELEGATION_GUIDANCE = (
    " You are a lean supervisor. For any specialized work, web research, reading or writing files, "
    "running code, or anything needing a domain tool, you have almost no direct tools, so you MUST "
    "delegate."
)


def assemble_system_message(config: AssistantConfig, agent_name: str, toolsets: Sequence[Toolset]) -> str:
    """One agent's full system message: its declared opener plus the guidance it earned.

    Guidance travels with the capability that needs it, so installing a toolset brings the instructions
    that make the model use it and removing one takes them away. Nothing here is conditional on a
    setting; it is conditional only on what the agent declares.
    """
    agent = config.agents[agent_name]
    parts = [agent.system_message or DEFAULT_SYSTEM_MESSAGE]
    parts.extend(toolset.guidance for toolset in toolsets if toolset.guidance)
    if agent.delegates_to:
        parts.append(DELEGATION_GUIDANCE)
        if all(toolset.cross_cutting for toolset in toolsets):
            parts.append(LEAN_DELEGATION_GUIDANCE)
    return "".join(parts)


def build_agent_specs(config: AssistantConfig, state: LiveState, delegator: str) -> dict[str, dict]:
    """AIMU ``agent_types`` for one delegator: a spec per agent it names in ``delegates_to``.

    Recursion is Kokua's rather than AIMU's. AIMU's own ``max_depth`` gives every depth the same menu,
    which cannot express a graph where each agent has its own targets, so a target that delegates gets
    its own delegate injected into its spec tools and AIMU is called with ``max_depth=1`` at every
    level. ``validate_agents`` has already proved the graph acyclic, which is what makes this recursion
    terminate: every recursive call moves to a target strictly further from ``delegator`` along a path
    with no repeated agent, and the graph has finitely many agents.
    """
    specs: dict[str, dict] = {}
    for name in config.agents[delegator].delegates_to:
        agent = config.agents[name]
        toolsets = select(agent.tools, state.registry, agent=name, entry_point=config.entry_agent)
        # A spawned worker is a plain AIMU Agent, so `agent=None`: the one toolset needing the live
        # agent object is entry-point-only and validation has already rejected it here.
        tools = build_tools(toolsets, ToolsetContext(state=state, agent=None))
        if agent.delegates_to:
            tools = tools + [_spawn_tool(config, state, name)]
        # AIMU reads the first line of a spec's system_message as that agent_type's menu label (see
        # _subagent_first_line). assemble_system_message's opener is one continuous paragraph with no
        # line break, which would make a useless label, so the description leads on its own line;
        # falling back to the agent's own name means an agent that skips `description` still gets a
        # label instead of a blank one.
        message = assemble_system_message(config, name, toolsets)
        specs[name] = {
            "system_message": f"{agent.description or name}\n\n{message}",
            "tools": tools,
        }
    return specs


def _spawn_tool(config: AssistantConfig, state: LiveState, delegator: str) -> Callable:
    """The ``spawn_subagent`` delegate for one agent, over that agent's own targets."""
    return make_async_subagent_tool(
        config.model,
        agent_types=build_agent_specs(config, state, delegator),
        tool_approval=state.tool_approval,
        observer=state.observer,
    )


def make_delegation_tool(agent, config: AssistantConfig, state: LiveState) -> Optional[Callable]:
    """The delegate for a live agent, or None when it declares no targets.

    Uses the agent's own model rather than ``config.model`` so a runtime model switch reaches the
    workers it spawns.
    """
    name = getattr(agent, "name", config.entry_agent)
    if not config.agents[name].delegates_to:
        return None
    return make_async_subagent_tool(
        agent.model_client.model,
        agent_types=build_agent_specs(config, state, name),
        tool_approval=state.tool_approval,
        observer=state.observer,
    )


def unreferenced_toolsets(config: AssistantConfig, registry: Mapping[str, Toolset]) -> list[str]:
    """Provisioned toolsets no agent names, for the startup warning.

    A toolset nobody names reaches no agent, and a plugin or MCP server in that position still cost
    something to load or connect to be unreachable, which is worth one line in the log rather than
    silence. A built-in AIMU group or a core subsystem toolset is excluded: it ships whether or not any
    agent declares it, so its being unnamed says nothing about a mistake -- unlike a name the user
    actually provisioned by installing a plugin or configuring a server, which was named specifically so
    something could reach it.
    """
    declared = {name for agent in config.agents.values() for name in agent.tools}
    providers = getattr(registry, "providers", {})
    return sorted(
        name for name in registry if name not in declared and providers.get(name) not in _UNPROVISIONED_PROVIDERS
    )
