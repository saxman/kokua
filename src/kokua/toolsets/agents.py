"""Assembling the registry from every provider, and resolving one agent's declared names.

Provider order fixes which label a collision message blames first and is otherwise arbitrary: a
collision is an error, not a precedence rule, so nothing here depends on one provider winning.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Callable, Mapping, Optional, Sequence

from aimu.aio.tools.builtin import SubagentObserver, make_async_subagent_tool

from kokua.config.file import ConfigError
from kokua.config.schema import DEFAULT_SYSTEM_MESSAGE, AssistantConfig
from kokua.plugins import discover_toolsets, own_distribution_toolset_names
from kokua.toolsets.builtin import BUILTIN_TOOLSETS
from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.core import CORE_TOOLSETS
from kokua.toolsets.registry import (
    Toolset,
    ToolsetError,
    ToolsetRegistry,
    build_tools,
    register,
    select,
    workflows_of,
)
from kokua.workflows import Workflow

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
# tell a name nobody provisioned (a built-in AIMU group, a core subsystem, one of Kokua's own shipped
# plugin toolsets) from a name the user actually provisioned (a third-party plugin, or a configured MCP
# server) without duplicating the strings.
_AIMU_PROVIDER = "AIMU capability"
_CORE_PROVIDER = "core subsystem"
_BUILTIN_PLUGIN_PROVIDER = "built-in toolset"
_PLUGIN_PROVIDER = "plugin"
_MCP_PROVIDER = "MCP server"
_SKILL_PROVIDER = "skill"

# Providers whose toolsets ship regardless of what any agent declares, so a name from one of these being
# unreferenced is not news -- unlike a third-party plugin the user installed, or a server the user
# configured, which earn a spot in the [agents.*] tables specifically so they can be reached. Skills
# belong here too, for a different reason: an entry agent holding the authoring toolset reaches every
# skill through its own catalogue, so a skill no [agents.*] table names is still usable.
_UNPROVISIONED_PROVIDERS = {_AIMU_PROVIDER, _CORE_PROVIDER, _BUILTIN_PLUGIN_PROVIDER, _SKILL_PROVIDER}


def _server_tools(url: str, state: LiveState) -> list:
    """A configured server's live tools, empty when it is not currently connected.

    Looked up at build time rather than snapshotted at registration, so the registry stays a pure
    function of config while a reconnect or a runtime removal still reaches the next rebuild.
    """
    for connection in state.connections:
        if getattr(connection, "url", None) == url:
            return list(getattr(connection, "callables", []))
    return []


def mcp_toolsets(config: AssistantConfig) -> list[Toolset]:
    """One toolset per configured MCP server, named by the server's ``name``."""
    return [
        Toolset(
            name=server.name,
            description=f"Tools from the MCP server at {server.url}.",
            build=lambda ctx, _url=server.url: _server_tools(_url, ctx.state),
        )
        for server in config.mcp_servers
    ]


def skill_toolsets(config: AssistantConfig) -> list[Toolset]:
    """One toolset per skill on disk, so a skill name is an entry in the same namespace as everything else.

    An agent declares ``"citation-check"`` beside ``"web"`` and does not say which kind each is, which is
    the point of the single namespace. ``build`` reads the live per-skill tool map rather than closing over
    tools here, keeping the registry a pure function of config the way the MCP source does; ``guidance``
    carries the skill's catalogue entry, so declaring a skill tells the holder it exists and how to load
    its instructions.

    Discovery runs its own ``SkillManager`` because the registry is built before ``LiveState`` exists (the
    registry is an argument to it). That is a second filesystem scan of one directory, which is cheaper
    than threading state into a function whose purity is load-bearing.
    """
    from aimu.skills import SkillManager

    skills = SkillManager(skill_dirs=[str(config.skills_dir)]).skills
    return [
        Toolset(
            name=skill.name,
            description=skill.description,
            build=lambda ctx, _name=skill.name: list(ctx.state.skill_tools.get(_name, [])),
            guidance=(
                f" The {skill.name!r} skill is available: {skill.description} "
                f"Call `activate_skill('{skill.name}')` to load its full instructions before acting on it."
            ),
        )
        for skill in skills.values()
    ]


def without_skill_names(names: Sequence[str], registry: ToolsetRegistry) -> list[str]:
    """``names`` with the skills removed, in order.

    For an agent that is a ``SkillAgent``: AIMU already gives it the catalogue and script tools of every
    skill in its manager, so resolving those names as toolsets as well would duplicate the catalogue in
    its prompt. A plain agent has no such machinery and resolves them normally.
    """
    return [name for name in names if registry.providers.get(name) != _SKILL_PROVIDER]


def build_registry(config: AssistantConfig) -> ToolsetRegistry:
    """Every toolset an agent may name, by name.

    Plugin discovery is gated on ``config.load_plugins``, which is a "do not execute third-party code"
    switch rather than a naming switch: with it off, a config naming a plugin toolset fails at startup
    with the unknown-name error rather than starting an agent quietly missing a capability.

    Discovered plugins are split by which distribution registered them: Kokua's own five (example,
    aimu_agents, pdf, image, email) get ``_BUILTIN_PLUGIN_PROVIDER`` rather than ``_PLUGIN_PROVIDER``, so
    ``unreferenced_toolsets`` does not warn about ships-in-the-box toolsets the shipped config simply
    never named. Both groups are still built with the same failure tolerance: the split is about what
    counts as news when unreferenced, not about how much this codebase trusts its own plugin code.
    """
    discovered = discover_toolsets()
    own_names = own_distribution_toolset_names()
    sources: list[tuple[str, list[Toolset]]] = [
        (_AIMU_PROVIDER, list(BUILTIN_TOOLSETS)),
        (_CORE_PROVIDER, list(CORE_TOOLSETS)),
        (_MCP_PROVIDER, mcp_toolsets(config)),
        (_SKILL_PROVIDER, skill_toolsets(config)),
    ]
    if config.load_plugins:
        sources.append(
            (_BUILTIN_PLUGIN_PROVIDER, [_tolerate_build_failures(t) for n, t in discovered.items() if n in own_names])
        )
        sources.append(
            (_PLUGIN_PROVIDER, [_tolerate_build_failures(t) for n, t in discovered.items() if n not in own_names])
        )
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
            "no agents configured: config.toml needs at least one [agents.<name>] table. Add one by "
            "hand -- config.example.toml, shipped with this install, has four to copy from -- or run "
            "`kokua config init --force` to overwrite this file with that shipped example."
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


def validated_registry(config: AssistantConfig) -> ToolsetRegistry:
    """The registry ``config`` resolves against, with its agents already checked against it.

    The two halves ship as one call because a registry nothing was validated against only defers the
    error to a worse moment. A front end that builds its assistant lazily (the web one builds per
    connection) calls this at startup to report a broken ``[agents.*]`` table where the user is looking.

    A ``ToolsetError`` is translated on the way out, so a front end has one ``ConfigError`` family to
    catch for everything wrong with config.toml rather than the registry's internal exception type.
    """
    try:
        registry = build_registry(config)
    except ToolsetError as error:
        raise ConfigError(str(error)) from error
    validate_agents(config, registry)
    return registry


# Commands the serve loop owns and a workflow therefore cannot claim. Checked at startup, because a
# workflow silently shadowed by /stop would look like a workflow that simply never runs.
RESERVED_COMMANDS = frozenset({"stop", "diag"})


def build_command_map(config: AssistantConfig, registry: ToolsetRegistry) -> dict[str, "Workflow"]:
    """The commands the entry agent's declared toolsets offer, by command word.

    Only the entry agent's, because a command arrives on the channel and only that agent has one; a
    spawned worker declaring a workflow-bearing toolset gets the toolset's tools and no command.

    Collisions raise rather than resolve. A workflow shadowed by a reserved command, or by another
    workflow, would present as one that inexplicably never runs, and the config naming both is the
    thing that has to change.

    A command's shape is checked here too, for the same reason: dispatch (``Assistant._serve_channel``)
    lowercases the incoming text and matches a single whitespace-free token, so a command that is empty,
    contains whitespace, or is not already lowercase could never be reached -- the exact "inexplicably
    never runs" failure mode this function otherwise guards against, just caused by the command's own
    shape instead of a collision with another one.
    """
    entry = config.agents[config.entry_agent]
    toolsets = select(entry.tools, registry, agent=config.entry_agent, entry_point=config.entry_agent)
    commands: dict[str, Workflow] = {}
    claimed_by: dict[str, str] = {}
    for toolset_name, workflow in workflows_of(toolsets):
        command = workflow.command
        if not command or command != command.lower() or any(ch.isspace() for ch in command):
            raise ConfigError(
                f"toolset {toolset_name!r} offers the command {command!r}, which is not a single "
                "lowercase word with no whitespace. Dispatch only ever matches a lowercased, "
                "whitespace-free token, so a command in any other shape is unreachable. Rename the "
                "workflow's command."
            )
        if command in RESERVED_COMMANDS:
            raise ConfigError(
                f"toolset {toolset_name!r} offers the /{command} command, which is reserved by "
                "the assistant itself. Rename the workflow's command."
            )
        if command in commands:
            raise ConfigError(
                f"toolsets {claimed_by[command]!r} and {toolset_name!r} both offer the "
                f"/{command} command. Rename one, or drop one from "
                f"[agents.{config.entry_agent}].tools."
            )
        commands[command] = workflow
        claimed_by[command] = toolset_name
    return commands


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
    """One agent's full system message: its opener plus the guidance it earned.

    Guidance travels with the capability that needs it, so installing a toolset brings the instructions
    that make the model use it and removing one takes them away. Nothing here is conditional on a
    setting except the opener itself; the guidance is conditional only on what the agent declares.

    The opener is the entry agent's own business only: ``--system`` (``config.system_message_override``)
    wins there over its declared ``system_message``, since a prompt is not the capability this design
    made ``[agents.*]`` the single source of. It never touches a worker's own declared opener, since the
    flag overrides the message of the agent the user is talking to, not every agent Kokua builds. Absent
    an override, an agent's own ``system_message`` wins, falling back to ``[assistant].system_message`` so
    that key keeps meaning what it always did: the opener for an agent that declares none of its own.
    """
    agent = config.agents[agent_name]
    if agent_name == config.entry_agent and config.system_message_override is not None:
        opener = config.system_message_override
    else:
        opener = agent.system_message or config.system_message or DEFAULT_SYSTEM_MESSAGE
    parts = [opener]
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
    observer: Optional[SubagentObserver] = state.observer
    return make_async_subagent_tool(
        config.model,
        agent_types=build_agent_specs(config, state, delegator),
        tool_approval=state.tool_approval,
        observer=observer,
    )


def make_delegation_tool(agent, config: AssistantConfig, state: LiveState) -> Optional[Callable]:
    """The delegate for a live agent, or None when it declares no targets.

    Uses the agent's own model rather than ``config.model`` so a runtime model switch reaches the
    workers it spawns.
    """
    name = getattr(agent, "name", config.entry_agent)
    if not config.agents[name].delegates_to:
        return None
    observer: Optional[SubagentObserver] = state.observer
    return make_async_subagent_tool(
        agent.model_client.model,
        agent_types=build_agent_specs(config, state, name),
        tool_approval=state.tool_approval,
        observer=observer,
    )


def unreferenced_toolsets(config: AssistantConfig, registry: ToolsetRegistry) -> list[str]:
    """Provisioned toolsets no agent names, for the startup warning.

    A toolset nobody names reaches no agent, and a plugin or MCP server in that position still cost
    something to load or connect to be unreachable, which is worth one line in the log rather than
    silence. A built-in AIMU group or a core subsystem toolset is excluded: it ships whether or not any
    agent declares it, so its being unnamed says nothing about a mistake -- unlike a name the user
    actually provisioned by installing a plugin or configuring a server, which was named specifically so
    something could reach it.

    Takes the concrete ``ToolsetRegistry`` (not a bare ``Mapping``) so ``registry.providers`` is
    guaranteed rather than an optional attribute: every real caller builds the registry with
    ``build_registry``, and a caller that didn't would have nothing meaningful to warn about anyway, so
    a missing provider map should fail loudly here rather than be read as "nothing is provisioned" and
    warn about every built-in group.
    """
    declared = {name for agent in config.agents.values() for name in agent.tools}
    return sorted(
        name
        for name in registry
        if name not in declared and registry.providers.get(name) not in _UNPROVISIONED_PROVIDERS
    )
