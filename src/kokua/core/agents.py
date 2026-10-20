"""Assembling the registry from every provider, and resolving one agent's declared names.

Registration order fixes which label a collision message blames first and is otherwise arbitrary: a
collision is an error, not a precedence rule, so no registered toolset ever loses to another by name.
Tool names are a different matter (``build_tools`` keeps the first of two same-named tools), which is
why ``available = "all"`` expands in :data:`TRUST_ORDER` rather than in registration order.
"""

from __future__ import annotations

import difflib
import functools
import logging
from dataclasses import dataclass
from typing import Callable, Collection, Mapping, Optional, Sequence

from aimu import trim_messages
from aimu.aio.tools.builtin import SubagentObserver, make_async_subagent_tool
from aimu.tools.groups import CORE_TOOL_NAMES

from kokua.config.file import ConfigError, check_resident_within_available
from kokua.config.schema import ALL_TOOLSETS, DEFAULT_SYSTEM_MESSAGE, AssistantConfig
from kokua.core import conversation_commands
from kokua.core.messaging import EVERYONE, USER, WORKER_SOURCE
from kokua.core.metrics import record_event
from kokua.plugins import discover_toolsets, own_distribution_toolset_names
from kokua.registry.context import LiveState, ToolsetContext
from kokua.registry.registry import (
    RESERVED_GATE_NAMESPACE,
    Toolset,
    ToolsetError,
    ToolsetRegistry,
    build_tools,
    entry_point_only_reason,
    register,
    select,
    workflows_of,
)
from kokua.workflows import Workflow

logger = logging.getLogger(__name__)

# Provider labels `build_registry` hands to `register`. These are what `--list-toolsets` groups by, and
# what a collision message names, so they are the words a user reads rather than an internal taxonomy.
# They decide behavior in two places, each a question no name inspection could answer: `is_skill` tells
# a skill's registry entry from every other kind by `_SKILL_PROVIDER` (which is also how
# `with_skill_toolsets` knows which entries to replace), and `available_for` orders an "all"
# declaration by `TRUST_ORDER`.
_BUILTIN_PLUGIN_PROVIDER = "built-in toolset"
_PLUGIN_PROVIDER = "plugin"
_MCP_PROVIDER = "MCP server"
_SKILL_PROVIDER = "skill"

#: The order ``available = "all"`` lists toolsets in, most trusted first: Kokua's own code, then a third
#: party's installed code, then skills (prose and scripts a person approved), then MCP servers, whose tools
#: and descriptions are whatever a remote host sends. The order decides which of two same-named tools wins
#: only among toolsets built together, because ``build_tools`` keeps the first: the resident build, and a
#: worker spec's resident tools. Registration order would have put MCP servers first there, letting a
#: remote server's ``read_file`` shadow the ``fs`` toolset's. It also orders the agent's tool groups. A
#: deferred toolset is settled by load order instead: AIMU keeps whichever tool the agent already holds,
#: skips the incoming one, and the load result names the skipped tool and its holder, so a later
#: load never replaces a held tool.
TRUST_ORDER: tuple[str, ...] = (_BUILTIN_PLUGIN_PROVIDER, _PLUGIN_PROVIDER, _SKILL_PROVIDER, _MCP_PROVIDER)


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
            description=server.description or f"Tools from the MCP server at {server.url}.",
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


def with_skill_toolsets(registry: ToolsetRegistry, toolsets: Sequence[Toolset]) -> ToolsetRegistry:
    """``registry`` with every skill entry replaced by ``toolsets`` and every other entry kept as it is.

    Skills are the one source that changes mid-session through a tool the assistant holds, so they are
    the one source re-read. Every other entry is carried over untouched rather than re-discovered: the MCP
    entries in particular stay the ones read from config at startup, so a server added at runtime still
    reaches an ``"all"`` agent only after a restart, exactly as it did before skills could refresh.

    Raises ``ToolsetError`` (from ``register``) when a skill now on disk collides with another toolset's
    name, leaving the caller's registry as it was.
    """
    kept: dict[str, list[Toolset]] = {}
    for name, toolset in registry.items():
        label = registry.providers[name]
        if label != _SKILL_PROVIDER:
            kept.setdefault(label, []).append(toolset)
    return register([*kept.items(), (_SKILL_PROVIDER, list(toolsets))])


def is_skill(name: str, registry: ToolsetRegistry) -> bool:
    """Whether ``name`` is registered as a skill, as opposed to any other kind of toolset or nothing."""
    return registry.providers.get(name) == _SKILL_PROVIDER


def without_skill_names(names: Sequence[str], registry: ToolsetRegistry) -> list[str]:
    """``names`` with the skills removed, in order.

    For an agent that is a ``SkillAgent``: AIMU already gives it the catalogue and script tools of every
    skill in its manager, so resolving those names as toolsets as well would duplicate the catalogue in
    its prompt. A plain agent has no such machinery and resolves them normally.
    """
    return [name for name in names if not is_skill(name, registry)]


def available_for(config: AssistantConfig, registry: ToolsetRegistry, agent_name: str) -> list[str]:
    """The toolset names ``agent_name`` may use: its declared list as written, or ``"all"`` expanded.

    ``"all"`` is every registered toolset in :data:`TRUST_ORDER`, registration order kept within one
    provider. Where two toolsets built together offer one tool name, the more trusted one's tool is kept;
    a toolset loaded later keeps whichever tool arrived first, and the load result names the skipped tool
    and its holder (see :data:`TRUST_ORDER`). An ``entry_point_only`` toolset is left out on any agent but the entry
    agent, rather than listed and then refused by validation for a name the declaration never wrote. A
    provider label outside ``TRUST_ORDER`` ranks last; only a registry assembled by hand, as a test does, can carry one.

    Every reader of an agent's availability goes through this, because the declaration alone cannot be
    iterated: ``ALL_TOOLSETS`` raises rather than pretending to be a list.
    """
    declared = config.agents[agent_name].available
    if declared is not ALL_TOOLSETS:
        return list(declared)

    def rank(name: str) -> int:
        provider = registry.providers.get(name)
        return TRUST_ORDER.index(provider) if provider in TRUST_ORDER else len(TRUST_ORDER)

    is_entry = agent_name == config.entry_agent
    return sorted(
        (name for name, toolset in registry.items() if is_entry or not toolset.entry_point_only),
        key=rank,
    )


def resident_for(config: AssistantConfig, registry: ToolsetRegistry, agent_name: str) -> list[str]:
    """The toolsets ``agent_name`` holds from its first turn: its declared ``resident`` list, else all of
    its availability.

    Always in availability order, by filtering :func:`available_for` rather than returning the declared
    list, so the order a user wrote ``resident`` in never changes the order tools are presented in.
    """
    agent = config.agents.get(agent_name)
    if agent is None:
        return []
    available = available_for(config, registry, agent_name)
    if agent.resident is None:
        return available
    return [entry for entry in available if entry in agent.resident]


@dataclass(frozen=True)
class ShadowedTool:
    """A resident tool one agent never sees because an earlier resident toolset offers the same name."""

    agent: str
    tool: str
    kept: str
    shadowed: str


def shadowed_tools(
    config: AssistantConfig, registry: ToolsetRegistry, tools_by_toolset: Mapping[str, Collection[str]]
) -> list[ShadowedTool]:
    """Every resident tool a same-named tool from an earlier toolset hides, per declared agent.

    ``build_tools`` keeps the first of two same-named tools, in the agent's resident order, and says
    nothing of the other; with ``available = "all"`` that order is trust order, so a third-party or MCP
    tool can silently lose to one Kokua ships. ``tools_by_toolset`` records what each toolset offered,
    which is all this needs. The entry agent's skills are held back as wiring holds them back, since its
    ``SkillAgent`` supplies those tools by another route and they are never competing for a name here.
    Every skill's build carries the one shared ``activate_skill``, so a second resident skill offering
    it again hides nothing, and it is left out of each skill's names, as :func:`gateable_tools` does.

    Startup builds the entry agent's resident toolsets and those of every worker reachable from it by
    ``delegates_to``; an agent nothing delegates to is never built, so a resident toolset absent from
    ``tools_by_toolset`` has no recorded names. Such an agent is skipped and logged at INFO rather than
    reported as shadow-free, and its toolsets are not built just to look, because building has side
    effects. A toolset another agent built still counts, since the record is per toolset, not per agent.
    """
    shadows: list[ShadowedTool] = []
    for agent_name in config.agents:
        resident = resident_for(config, registry, agent_name)
        if agent_name == config.entry_agent:
            resident = without_skill_names(resident, registry)
        unbuilt = [toolset for toolset in resident if toolset not in tools_by_toolset]
        if unbuilt:
            logger.info("Not checking %s for shadowed tools: %s not built at startup.", agent_name, unbuilt)
            continue
        holder: dict[str, str] = {}
        for toolset in resident:
            tools = set(tools_by_toolset.get(toolset, ()))
            if is_skill(toolset, registry):
                tools.discard(_ACTIVATE_SKILL)
            for tool in sorted(tools):
                if tool in holder:
                    shadows.append(ShadowedTool(agent_name, tool, holder[tool], toolset))
                else:
                    holder[tool] = toolset
    return shadows


def deferred_for(config: AssistantConfig, registry: ToolsetRegistry, agent_name: str) -> list[str]:
    """The toolsets ``agent_name`` can load but does not hold yet: its availability minus resident, in order."""
    if agent_name not in config.agents:
        return []
    resident = set(resident_for(config, registry, agent_name))
    return [entry for entry in available_for(config, registry, agent_name) if entry not in resident]


def loadable_toolsets(config: AssistantConfig, registry: ToolsetRegistry, agent_name: str) -> list[str]:
    """The toolsets ``agent_name`` loads on demand, divided exactly as wiring divides them.

    :func:`deferred_for` counts a skill the entry agent names outside ``resident``. Wiring does not: the
    entry agent is a ``SkillAgent`` that already holds every skill, so ``wire_agent`` holds skill names
    back before the residency split. This is what decides whether an agent carries the loader tools at
    all. Whether an approval entry waits for a build is a different question, answered by
    :func:`built_toolsets`.
    """
    deferred = deferred_for(config, registry, agent_name)
    if agent_name == config.entry_agent:
        return without_skill_names(deferred, registry)
    return deferred


def build_registry(config: AssistantConfig) -> ToolsetRegistry:
    """Every toolset an agent may name, by name.

    Every installed entry point is registered, unconditionally. There is no switch, because installing a
    distribution that registers one is the consent, and the switch that used to exist could not do what
    it claimed: ``resolve_config`` imports every entry point before parsing the file, to learn which
    config sections are legal, so withholding the names afterwards executed the same code and only
    turned a working ``tools`` declaration into an unknown-toolset error. What an agent may *use* is
    still exactly what its own table declares; this function decides what a name can resolve to, not
    what any agent holds.

    Three routes, and only one of them carries a toolset: the ``kokua.toolsets`` entry-point group, which
    is how every toolset Kokua ships registers *and* how a third party's does. There is no directory scan
    and no list in code, so nothing here decides which toolsets exist; ``pyproject.toml`` does, and
    ``tests/toolsets/test_registration.py`` pins that table against ``src/kokua/toolsets/`` in both
    directions.

    The two entry-point labels split Kokua's own from a third party's, by which distribution registered
    the entry point. ``--list-toolsets`` groups by that split, and :data:`TRUST_ORDER` ranks on it, so an
    ``available = "all"`` agent lists Kokua's own toolsets ahead of a third party's. Every toolset
    keeps the ``build`` its author wrote, whichever route it came by, because a build failure is
    a bug in whoever wrote the toolset and a warning in a log file is not how a person finds out an agent
    lost a capability.
    """
    discovered = discover_toolsets()
    own_names = own_distribution_toolset_names()
    sources: list[tuple[str, list[Toolset]]] = [
        (_MCP_PROVIDER, mcp_toolsets(config)),
        (_SKILL_PROVIDER, skill_toolsets(config)),
        (_BUILTIN_PLUGIN_PROVIDER, [t for n, t in discovered.items() if n in own_names]),
        (_PLUGIN_PROVIDER, [t for n, t in discovered.items() if n not in own_names]),
    ]
    return register(sources)


#: Agent names the message bus has already spent. An agent's declared name becomes the address a
#: mid-turn message is sent to (``core/messaging.py``'s ``_register``), so these two collide rather
#: than merely confuse: an entry agent named ``user`` mints the user's own address, and ``close``
#: would re-run that agent's messages as the user's own words; one named ``everyone`` can never be
#: addressed alone, since ``matches`` answers the broadcast before it looks at an address. Refused at
#: startup rather than at reader-open time, because the collision is in the config and is silent
#: everywhere else.
RESERVED_AGENT_NAMES = frozenset({USER, EVERYONE})
#: Characters no declared agent name may contain: the approval gate binds composed workers and the default
#: tier under names built from them, and a declared agent must never be able to equal one.
RESERVED_GATE_NAME_CHARACTERS = (":", "[")


def validate_agents(config: AssistantConfig, registry: ToolsetRegistry) -> None:
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
        if any(character in name for character in RESERVED_GATE_NAME_CHARACTERS):
            raise ConfigError(
                f"[agents.{name!r}] contains ':' or '[', which are reserved for the approval gate's own "
                "names (a composed worker is gated as 'composed:<label>' and the default tier as "
                "'[assistant.tools]'). An agent with such a name could be mistaken for one of those, so a "
                "worker composed to look like it would inherit its approval policy. Rename the agent."
            )
        if name in RESERVED_AGENT_NAMES:
            raise ConfigError(
                f"[agents.{name}] uses a name reserved by the message bus. An agent's declared name is "
                f"the address a message is sent to, and {name!r} already means something there: "
                f"{USER!r} is the user's own address and {EVERYONE!r} reaches every run. Rename the agent."
            )
        if agent.available is ALL_TOOLSETS:
            _check_resident_within_all(config, registry, name)
        try:
            select(available_for(config, registry, name), registry, agent=name, entry_point=config.entry_agent)
        except ToolsetError as e:
            raise ConfigError(str(e)) from e
        _check_model(f"agents.{name}", agent.model)
        for target in agent.delegates_to:
            if target not in config.agents:
                known = ", ".join(sorted(config.agents))
                raise ConfigError(f"agent {name!r} delegates to unknown agent {target!r}. Configured agents: {known}.")
    _reject_cycles(config)


def _check_resident_within_all(config: AssistantConfig, registry: ToolsetRegistry, name: str) -> None:
    """Refuse a ``resident`` entry an ``available = "all"`` agent does not hold, saying why it does not.

    The config parser checks ``resident`` against a declared ``available`` list, but it cannot see the
    registry that says what ``"all"`` holds, so this half of the check waits for the expansion. An entry
    is outside it for one of two reasons, and each gets its own sentence: nothing installs it, or it is
    an ``entry_point_only`` toolset that ``"all"`` leaves out on a worker.
    """
    agent = config.agents[name]
    # The parser refuses "all" with no resident list, but a config built without it (update_config's dry
    # run) arrives here unrefused, and would otherwise make every installed toolset resident.
    check_resident_within_available(name, agent.resident, agent.available)
    expanded = available_for(config, registry, name)
    for entry in agent.resident:
        if entry in expanded:
            continue
        section = f"[agents.{name}.tools].resident"
        if entry in registry:
            raise ConfigError(
                f"{section} names {entry!r}, {entry_point_only_reason(config.entry_agent)} "
                'available = "all" leaves it out here, so drop it from resident.'
            )
        installed = ", ".join(sorted(registry)) or "(none)"
        raise ConfigError(
            f'{section} names {entry!r}, which is not an installed toolset, so available = "all" does not '
            f"hold it. Installed toolsets: {installed}."
        )


def validate_reviewers(config: AssistantConfig) -> None:
    """Reject a declared ``[reviewers.<name>].model`` AIMU cannot resolve, before anything asks it a
    question.

    A separate function from ``validate_agents`` rather than one more loop inside it: that function's
    own docstring is about the delegation graph an agent belongs to (acyclicity, unknown delegates,
    unknown toolsets), none of which a reviewer has. A reviewer is not an agent and takes none of an
    agent's three reach-granting keys (see ``ReviewerConfig``), so folding this loop into
    ``validate_agents`` would answer a question that function was never about.

    Every declared reviewer is checked, not only the ones ``[security.auto_approval].reviewers`` names,
    and whether or not that gate is even on. Two reasons: ``[reviewers.plan]`` and ``[reviewers.result]``
    are deep planning's critics and have nothing to do with the gate, so limiting this to the gate's own
    list would leave them unchecked; and an agent's own model is validated unconditionally, regardless of
    whether anything currently delegates to it, so a reviewer nothing currently asks matches that same
    rule rather than being a special case.

    Only the declared value, never the resolved one: a reviewer that declares no model inherits
    ``[assistant].model`` (see ``AssistantConfig.reviewer_for``), which is validated on its own path when
    it is set, and re-checking the inherited value here would either duplicate that error or, if
    ``[assistant].model`` is itself unset, force a resolution this function has no business triggering
    just to check it. ``core/auto_approval.py``'s own ``thinking`` check reads the same declared field
    for the same reason: a value this table did not write is not this table's mistake to report.
    """
    for name, reviewer in config.reviewers.items():
        _check_model(f"reviewers.{name}", reviewer.model)


def validated_registry(config: AssistantConfig) -> ToolsetRegistry:
    """The registry ``config`` resolves against, with its agents and reviewers already checked against it.

    The halves ship as one call because a registry nothing was validated against only defers the error
    to a worse moment. A front end that builds its assistant lazily (the web one builds per connection)
    calls this at startup to report a broken ``[agents.*]`` or ``[reviewers.*]`` table where the user is
    looking.

    A ``ToolsetError`` is translated on the way out, so a front end has one ``ConfigError`` family to
    catch for everything wrong with config.toml rather than the registry's internal exception type.
    """
    try:
        registry = build_registry(config)
    except ToolsetError as error:
        raise ConfigError(str(error)) from error
    validate_agents(config, registry)
    validate_reviewers(config)
    return registry


# Commands the serve loop owns and a workflow therefore cannot claim. Checked at startup, because a
# workflow silently shadowed by /stop would look like a workflow that simply never runs. The
# conversation commands are dispatched in the same loop and ahead of the workflow lookup, so they
# shadow exactly the same way and are read from the module that owns them rather than spelled twice.
RESERVED_COMMANDS = frozenset({"stop", "diag"}) | conversation_commands.COMMANDS


def build_command_map(config: AssistantConfig, registry: ToolsetRegistry) -> dict[str, "Workflow"]:
    """The commands the entry agent's declared toolsets offer, by command word.

    Only the entry agent's, because a command arrives on the channel and only that agent has one; a
    spawned worker declaring a workflow-bearing toolset gets the toolset's tools and no command.

    Collisions raise rather than resolve. A workflow shadowed by a reserved command, or by another
    workflow, would present as one that inexplicably never runs, and the config naming both is the
    thing that has to change.

    A workflow must also share its carrying toolset's name, checked here because this is where the two
    are paired. A workflow's settings are the toolset's ``[<name>]`` section (``WorkflowContext.settings``
    resolves them by the workflow's name), so a second, disagreeing name would hand a workflow either
    nothing or another capability's section. That is why the rule is one name rather than a
    ``settings_key`` field: a field could disagree with the section the values actually came from.

    A command's shape is checked here too, for the same reason: dispatch (``Assistant._serve_channel``)
    lowercases the incoming text and matches a single whitespace-free token, so a command that is empty,
    contains whitespace, or is not already lowercase could never be reached -- the exact "inexplicably
    never runs" failure mode this function otherwise guards against, just caused by the command's own
    shape instead of a collision with another one.
    """
    available = available_for(config, registry, config.entry_agent)
    toolsets = select(available, registry, agent=config.entry_agent, entry_point=config.entry_agent)
    commands: dict[str, Workflow] = {}
    claimed_by: dict[str, str] = {}
    for toolset_name, workflow in workflows_of(toolsets):
        if workflow.name != toolset_name:
            raise ConfigError(
                f"toolset {toolset_name!r} carries workflow {workflow.name!r}; a workflow must share its "
                "toolset's name, since that is the config section its settings come from."
            )
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
                f"[agents.{config.entry_agent}.tools].available."
            )
        commands[command] = workflow
        claimed_by[command] = toolset_name
    return commands


def undeclared_workflow_commands(config: AssistantConfig, registry: ToolsetRegistry) -> dict[str, str]:
    """Every command a workflow-bearing toolset in ``registry`` offers that the entry agent did not
    declare, mapping the command word to the offering toolset's name.

    This is the gap ``build_command_map`` leaves on purpose: a config that predates naming a
    workflow's toolset (or dropped it) still gets the command typed at it verbatim -- the web Plan
    toggle sends ``"/plan <task>"`` over the socket regardless of what ``[agents.*.tools].available`` says -- so
    the serve loop needs to recognize the word even though no declared toolset claims it, to answer
    with what config change would grant it instead of running a plain turn on the literal command text.

    Not to be confused with :func:`configured_but_undeclared`, which shares only the word "undeclared":
    that one checks every agent's tools (a toolset a worker holds is still declared) and looks at config
    sections rather than commands, and fires once at startup rather than on an incoming channel message.
    """
    declared = set(available_for(config, registry, config.entry_agent))
    return {
        workflow.command: toolset_name
        for toolset_name, workflow in workflows_of(list(registry.values()))
        if toolset_name not in declared
    }


def configured_but_undeclared(config: AssistantConfig, registry: ToolsetRegistry) -> list[str]:
    """Every ``config.toml`` section a toolset owns that no agent's ``tools`` names, for a startup
    warning about the config file rather than about anything typed at the channel.

    Not to be confused with :func:`undeclared_workflow_commands`, a sibling that answers a different
    question: that one checks only the *entry* agent's tools (only it ever receives a channel command)
    and looks at *commands* an installed toolset's workflow offers, so the serve loop can still
    recognize ``/plan`` typed at it and name the fix instead of running a plain turn on the literal
    text. This one checks every agent's tools and looks at *config sections*, so a warning can fire the
    moment the assistant starts, before anyone has typed anything: writing ``[planning]`` and leaving
    ``planning`` out of every ``[agents.*.tools].available`` means its settings are read by nobody, which is
    otherwise silent until someone notices the checkboxes doing nothing.

    Checked against every agent's tools, not just the entry agent's: a section belongs to whichever
    toolset owns it regardless of which agent holds that toolset, so a worker-only declaration still
    counts as declared.

    Reads ``configured_sections`` rather than ``toolset_settings``: seeding fills a bucket for every
    toolset's declared default whether or not the file had a section for it, so ``toolset_settings``
    cannot tell "the user wrote this" from "Kokua defaulted it" -- exactly the distinction this warning
    needs, since a defaulted section nobody wrote is not a mistake worth reporting.
    """
    declared = {toolset for name in config.agents for toolset in available_for(config, registry, name)}
    return sorted(name for name in config.configured_sections if name not in declared)


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
#
# The closing sentences say what counts as specialized, and they are the counterweight to the trivia
# clause rather than a repeat of the lean one below. That clause names activities ("web research"), which
# only helps once the model has decided the question needs the web; a question it believes it already
# knows the answer to never gets that far. So the trigger stated here is epistemic (could the answer have
# moved, could the user check it) and the examples are categories of question, not tools the agent holds
# -- an enumeration of questions cannot go stale when config.toml changes, which is what rules the tool
# enumeration out. "Even when you think you know" is the operative half: a model's confidence is the
# unreliable signal, so the instruction deliberately does not ask it to consult that confidence.
DELEGATION_GUIDANCE = (
    " Answer trivial or conversational requests directly with your own tools. Delegate specialized work "
    "by calling `spawn_subagent(agent_type, task)`: pick the role that fits, give it a complete, "
    "self-contained task (it shares no history with you), then relay or synthesize its answer for the "
    "user. Emit several `spawn_subagent` calls when subtasks are independent. Treat a request as "
    "specialized whenever its answer could have changed since you were trained, or the user could check "
    "it against a source: current events, prices, releases, published figures, who holds a role, what a "
    "page says today. Delegate those instead of answering from memory, even when you think you know."
)

# Added only when every toolset the agent declared is cross_cutting. Without the "almost no direct
# tools" clause a lean agent answers web, file, and code questions from memory instead of spawning a
# worker that has the tools; without the "lean supervisor" framing preceding it, the sentence has no
# subject. Both halves are derived from the declaration rather than asserted unconditionally: an agent
# holding a domain toolset is neither a lean supervisor nor genuinely short on direct tools, so granting
# it one removes both claims instead of leaving the prompt contradicting the advertised tools.
#
# The arithmetic sentence is the delegating half of `compute`'s guidance, split the way the web pair is:
# that guidance reaches whoever holds the tools, and this reaches an agent that must hand the work on.
# "Running code" alone does not reach it, because a model writing a conversion into its reply does not
# think of itself as running code. It lives here rather than in DELEGATION_GUIDANCE because only here is
# the agent certain to hold no `compute`: that toolset is not cross_cutting, so a lean agent never has
# it, and the instruction cannot contradict the tool guidance of an agent that does.
LEAN_DELEGATION_GUIDANCE = (
    " You are a lean supervisor. For any specialized work, web research, reading or writing files, "
    "running code, or anything needing a domain tool, you have almost no direct tools, so you MUST "
    "delegate. That includes arithmetic: a figure worked out in your reply (a currency or unit "
    "conversion, a percentage, a span between dates) is an estimate, so delegate the calculation with "
    "the raw figures and their units, and relay the units the result comes back in."
)


def split_by_residency(
    config: AssistantConfig, registry: ToolsetRegistry, agent_name: str, toolsets: Sequence[Toolset]
) -> tuple[list[Toolset], list[Toolset]]:
    """``toolsets`` divided into what ``agent_name`` holds from its first turn and what it loads on demand.

    Both halves keep the order they arrived in. ``toolsets`` may be a subset of the agent's declaration
    (the entry agent's skill names are held back before this runs), so membership is decided by name
    against ``resident_for`` rather than by position.
    """
    resident_names = set(resident_for(config, registry, agent_name))
    resident = [toolset for toolset in toolsets if toolset.name in resident_names]
    deferred = [toolset for toolset in toolsets if toolset.name not in resident_names]
    return resident, deferred


def approval_note(confirm: list[str], review: list[str], toolset: str) -> str:
    """What the model is told about which of ``toolset``'s tools ask before they run, or "" for none.

    Shown beside a toolset in ``available_tools``, so a model choosing what to load knows a gated tool
    will stop for an answer before it plans around the tool. Entries speak the gate's own vocabulary
    (``toolset``, ``toolset.*``, ``toolset.tool``) and both lists count, since a reviewed call is a
    gated call that a person may still be asked about.
    """
    tools: set[str] = set()
    for entry in [*confirm, *review]:
        name, _, tool = entry.partition(_GATE_SEPARATOR)
        if name != toolset:
            continue
        if tool in ("", _GATE_WILDCARD):
            return "Asks first: every tool."
        tools.add(tool)
    if not tools:
        return ""
    return f"Asks first: {', '.join(sorted(tools))}."


def assemble_system_message(
    config: AssistantConfig,
    agent_name: str,
    toolsets: Sequence[Toolset],
    deferred: Sequence[Toolset] = (),
) -> str:
    """One agent's full system message: its opener plus the guidance it earned.

    ``toolsets`` are the ones the agent holds from its first turn, and only their guidance is written
    in: a deferred toolset's guidance arrives with the tools themselves, when the model loads them, so
    the prompt pays for nothing the agent is not yet using. The deferred ones are named in one line, so
    the model knows there is something to load, and they still count toward whether the agent is a lean
    supervisor: an agent that can load a domain toolset is not short on tools.

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
    if deferred:
        parts.append(f" Also available, load before use: {', '.join(toolset.name for toolset in deferred)}.")
    if agent.delegates_to:
        parts.append(DELEGATION_GUIDANCE)
        if all(toolset.cross_cutting for toolset in [*toolsets, *deferred]):
            parts.append(LEAN_DELEGATION_GUIDANCE)
    return "".join(parts)


# The share of a declared window left for a worker's messages. `trim_messages` counts messages alone,
# while the window it has to fit also holds that worker's system message, its tool schemas, and the reply
# still to be generated -- and a worker's tool block runs to several thousand tokens on its own. A
# fraction rather than a subtraction of those three sizes: two of them cannot be measured before the
# turn that needs them, and a subtraction of fixed guesses goes negative on a small window, which would
# silently stop compacting exactly where a window fills soonest.
_COMPACTION_WINDOW_SHARE = 0.75


def compaction_for_window(generation: Mapping) -> Optional[Callable[[list[dict]], list[dict]]]:
    """The trimmer a spawned worker applies before each of its model turns, or None to apply none.

    Engaged by a declared ``context_length`` and nothing else, because that key is the only place a
    window size is ever stated: no client reports the window it is talking to, so the alternative is
    Kokua guessing one and rewriting a worker's messages against a number nobody wrote down. A worker
    with no declared window therefore still fills it and still dies in it, and what it reports when it
    does is AIMU's own doing rather than Kokua's -- since 0.31.0 a spawn returns a tool result naming
    the *sub-agent's* window as the one that filled, where it used to hand the parent a message about
    "the conversation" that the parent read as its own.

    Only a spawned worker gets one. The entry agent's messages are the conversation the user is reading
    and Kokua persists, so trimming them would drop history that is still on screen, silently and for
    the rest of that conversation's life; a worker's messages are built per spawn and discarded with it,
    which is what makes an automatic rewrite of them safe to do at all.
    """
    window = generation.get("context_length")
    if not window:
        return None
    budget = int(window * _COMPACTION_WINDOW_SHARE)
    if budget <= 0:
        # A window whose message share rounds to nothing cannot hold a turn at all, so a trimmer built
        # from it could only delete. `context_length` is validated as an int of at least 1 and `bool` is
        # an int subclass, so `context_length = true` reaches here as a window of 1: a value the
        # schema's floor lets through and that this would otherwise turn into "drop everything
        # droppable before every turn". Declining leaves the reporting to AIMU, as an undeclared window
        # does.
        return None
    return lambda messages: trim_messages(messages, max_tokens=budget)


def build_agent_specs(config: AssistantConfig, state: LiveState, delegator: str) -> dict[str, dict]:
    """AIMU ``agent_types`` for one delegator: a spec per agent it names in ``delegates_to``.

    Recursion is Kokua's rather than AIMU's. AIMU's own ``max_depth`` gives every depth the same menu,
    which cannot express a graph where each agent has its own targets, so a target that delegates gets
    its own delegate injected into its spec tools and AIMU is called with ``max_depth=1`` at every
    level. ``validate_agents`` has already proved the graph acyclic, which is what makes this recursion
    terminate: every recursive call moves to a target strictly further from ``delegator`` along a path
    with no repeated agent, and the graph has finitely many agents.
    """
    # Imported here, not at module level: kokua.core.build imports this module lazily for the same cycle
    # resolve_system_message documents, so neither side may import the other at module scope.
    from kokua.core.build import tool_groups_for

    specs: dict[str, dict] = {}
    for name in config.agents[delegator].delegates_to:
        agent = config.agents[name]
        available = available_for(config, state.registry, name)
        toolsets = select(available, state.registry, agent=name, entry_point=config.entry_agent)
        resident, deferred = split_by_residency(config, state.registry, name, toolsets)
        # A spawned worker is a plain AIMU Agent, so `agent=None`: the one toolset needing the live
        # agent object is entry-point-only and validation has already rejected it here. The name is
        # carried regardless, so a toolset scoping itself to its holder (`benchmark`) resolves this
        # worker's own model rather than the delegator's.
        # A partial, so each worker's groups build with that worker's own context (a bare closure over a
        # loop variable would hand every group the last worker's).
        make_context = functools.partial(ToolsetContext, state=state, agent=None, agent_name=name)
        # Only the resident toolsets are built here. A deferred one is built by its group, when the
        # worker loads it, and that build goes through `build_tools` too, so recording and approval
        # cover it exactly as they cover a toolset built now.
        tools = build_tools(resident, make_context())
        if agent.delegates_to:
            tools = tools + [_spawn_tool(config, state, name)]
        # AIMU reads the first line of a spec's system_message as that agent_type's menu label (see
        # _subagent_first_line). assemble_system_message's opener is one continuous paragraph with no
        # line break, which would make a useless label, so the description leads on its own line;
        # falling back to the agent's own name means an agent that skips `description` still gets a
        # label instead of a blank one.
        message = assemble_system_message(config, name, resident, deferred)
        specs[name] = {
            "system_message": f"{agent.description or name}\n\n{message}",
            "tools": tools,
            "tool_groups": tool_groups_for(config, state, name, resident, deferred, make_context),
            # Bound to this worker's own name, so its calls are judged by its own policy rather than
            # its delegator's.
            "tool_approval": state.gate_for(name),
        }
        # Only a declared model is carried. AIMU reads a missing key as "the model the spawn tool was
        # built with", which is the [assistant].model default -- so an undeclared worker runs on that
        # default rather than inheriting whatever its delegator was pinned to.
        if agent.model:
            specs[name]["model"] = agent.model
        # Thinking is the reverse: the *resolved* value is written in, not just a declared one, because
        # the spawn tool has no thinking tier for a missing key to fall back to. AIMU reads a spec
        # without one as None, so an undeclared worker would skip the [assistant].thinking default
        # rather than inherit it. Tested against `is not None` so `thinking = false` reaches the spec.
        thinking = config.thinking_for(name)
        if thinking is not None:
            specs[name]["thinking"] = thinking
        # Resolved, not just declared, for the reason thinking is: AIMU reads a spec without the key as
        # "no generation parameters", so an undeclared worker would skip the [assistant.generation]
        # default rather than inherit it. Omitted entirely when nothing resolves, because an empty dict
        # is still a written tier and this one sits above the model card's own profile.
        generation = config.generation_for(name)
        if generation:
            specs[name]["generate_kwargs"] = generation
        # Declared only, like the model and unlike thinking or generation: AIMU reads a missing
        # max_iterations as "the cap the spawn tool was built with", which _spawn_tool sets to the
        # [assistant] default. So an undeclared worker inherits that default without Kokua writing it
        # in, and inherits it rather than its delegator's pin. Tested `is not None` rather than truthy,
        # so the parse-time floor stays the only thing with an opinion about the value.
        if agent.max_iterations is not None:
            specs[name]["max_iterations"] = agent.max_iterations
        # Resolved, like thinking and generation and for the same reason: `generation_for` has already
        # folded [assistant.generation] into this worker's own table, so the trimmer built here is the
        # worker's own declared window when it declares one and the global window otherwise, and neither
        # is ever its delegator's. Omitted when nothing resolves rather than written as None, which
        # AIMU reads by *membership*: a written None means "no compaction for this specialist whatever
        # the spawn tool was built with", a decision nobody made here. With the key absent the factory
        # tier below applies, and that tier is built from the same global window, so the two agree.
        compaction = compaction_for_window(config.generation_for(name))
        if compaction is not None:
            specs[name]["compaction"] = compaction
        # Written unconditionally, unlike every other key above, and so a deliberate exception to "a
        # capability is declared, never defaulted": the value is not a setting resolved per worker but
        # the one process-wide source that reads whichever turn is running when AIMU opens a reader
        # over it. A worker with no turn around it gets a drain that returns nothing, so there is no
        # tier for a missing key to fall back to and nothing a declaration could say differently.
        # `WORKER_SOURCE`, never `ENTRY_SOURCE`: a worker opens an independent cursor, so a
        # message only a worker consumed still comes back from `close` and runs as a follow-up turn
        # rather than counting as the conversation having seen it. Receiving and sending are coupled
        # through this one key: AIMU accepts a spec's own `"inbox": None` to turn a specialist's
        # reading off, and nothing here does that today, but it would also silently take away that
        # worker's ability to call `send_message` (`toolsets/messaging.py` refuses a run with no
        # `current_address`, which only an opened reader ever sets).
        specs[name]["inbox"] = WORKER_SOURCE
    return specs


def _check_model(table: str, model: Optional[str]) -> None:
    """Reject a declared model AIMU cannot resolve, naming the table it came from.

    ``table`` is the dotted path to blame in the error, ``"agents.<name>"`` or ``"reviewers.<name>"``:
    both callers share this function because resolving a string is the same question either way, and a
    shared check is what keeps the two messages from drifting apart. The caller has already decided
    what an unset ``model`` means (both fall back to ``[assistant].model``, validated on its own path)
    and picks the string to check before reaching here; this function never sees the unset case as
    anything but "nothing to check" (the guard below).

    Resolving the string is offline and cheap: no client is constructed, no key is read, and no weights
    load. Doing it here rather than at first use is what keeps a typo from surfacing mid-turn or mid-review,
    since a worker's model is only reached once something delegates to it, and a reviewer's only once
    something asks it a question. A provider whose optional dependency is not installed fails the same
    way, which is the same wall the client build would hit later.

    ``resolve_model``, not ``resolve_model_string``: only the former reads the full
    ``provider:model_id[@base_url][;flags]`` grammar that ``[assistant].model`` already accepts (that
    key is validated by building a throwaway client, which parses everything). The narrower resolver
    would refuse an endpoint the entry agent runs on happily, so pinning a worker or a reviewer to the
    host the assistant itself uses would fail at startup.
    """
    if not model:
        return
    from aimu.models.model_client import resolve_model

    try:
        resolve_model(model)
    except (ValueError, TypeError) as e:
        raise ConfigError(f"[{table}].model is {model!r}, which cannot be resolved: {e}") from e


def _spawn_tool(config: AssistantConfig, state: LiveState, delegator: str) -> Callable:
    """The ``spawn_subagent`` delegate for one agent, over that agent's own targets.

    ``events=record_event``, not a ``LiveState`` field: a spawned worker builds its own client, so
    without an explicit sink its model turns are invisible to whatever cost accounting the delegator
    keeps. ``record_event`` is a module-level constant that reads the running turn off a contextvar
    when an event fires, so a tool built once here reports into whichever turn is running at call time.

    ``max_iterations=config.max_iterations`` is the *global* default, deliberately not
    ``config.max_iterations_for(delegator)``. AIMU reads a spec without the key as "the cap this tool
    was built with", so passing the delegator's own resolved cap would make an undeclared worker inherit
    its delegator's pin, which is the one thing ``max_iterations_for`` promises not to do. Same trap as
    the model, one field over, and the same answer: ask the config for the default, not the caller for
    its own.
    """
    observer: Optional[SubagentObserver] = state.observer
    # Deferred: core.approval imports this module for the gate vocabulary.
    from kokua.core.approval import DEFAULT_POLICY

    # A fallback no Kokua spawn reaches: every spec carries its own gate and Kokua spawns with max_depth=1.
    return make_async_subagent_tool(
        config.default_model,
        agent_types=build_agent_specs(config, state, delegator),
        tool_approval=state.gate_for(DEFAULT_POLICY),
        observer=observer,
        events=record_event,
        max_iterations=config.max_iterations,
        compaction=compaction_for_window(config.generation),
    )


def make_delegation_tool(agent, config: AssistantConfig, state: LiveState) -> Optional[Callable]:
    """The delegate for a live agent, or None when it declares no targets.

    The tool is built with ``config.default_model``, not the delegator's own model: a worker declaring
    no model of its own runs on that default rather than inheriting a delegator's pin, and a worker
    that declares one carries it in its spec.

    Asking the config rather than the live agent is load-bearing, not a tidy-up. This read used to be
    ``config.model or agent.model_client.model``, which reached for the delegator's already-built
    client whenever ``[assistant].model`` was unset. A client answers that question with a resolved
    ``Model`` enum, so a default carrying an ``@base_url`` arrived here stripped of it, and every
    sub-agent was rebuilt against the provider default while the delegator kept talking to the
    override. See ``AssistantConfig.default_model``.

    ``max_iterations`` is read from the config for the same reason and with the same consequence: the
    global default, not the delegator's resolved cap, so a worker declaring no cap of its own gets
    ``[assistant].max_iterations`` rather than inheriting its delegator's.
    """
    name = getattr(agent, "name", config.entry_agent)
    if not config.agents[name].delegates_to:
        return None
    observer: Optional[SubagentObserver] = state.observer
    # Deferred: core.approval imports this module for the gate vocabulary.
    from kokua.core.approval import DEFAULT_POLICY

    # A fallback no Kokua spawn reaches: every spec carries its own gate and Kokua spawns with max_depth=1.
    return make_async_subagent_tool(
        config.default_model,
        agent_types=build_agent_specs(config, state, name),
        tool_approval=state.gate_for(DEFAULT_POLICY),
        observer=observer,
        events=record_event,
        max_iterations=config.max_iterations,
        compaction=compaction_for_window(config.generation),
    )


#: The one tool every skill's toolset carries a reference to. It is attributed to the reserved namespace
#: and removed from each skill, so gating one skill cannot stop every skill from being loaded.
_ACTIVATE_SKILL = "activate_skill"
_GATE_SEPARATOR = "."
_GATE_WILDCARD = "*"
#: The forms a gate entry may take, quoted back at the user in every error this section raises.
_GATE_GRAMMAR = (
    "Every entry names its toolset: 'compute' for every tool that capability provides, 'compute.*' for "
    "the same thing said with a wildcard, or 'compute.execute_python' for one tool. The reserved "
    f"{RESERVED_GATE_NAMESPACE!r} prefix holds the tools no toolset provides."
)


def gateable_tools(state: LiveState, entry_agent) -> dict[str, set[str]]:
    """Every tool a gate entry may name, under the prefix it has to be named with.

    Three sources, because a tool reaches an agent three ways and only one of them is the registry.
    ``state.tools_by_toolset`` holds what ``build_tools`` produced, already grouped by the toolset that
    offered it, for the entry agent and for every worker at every delegation depth. The entry agent's
    own list adds ``spawn_subagent``, attached after its toolsets are built. The skills an AIMU
    ``SkillAgent`` surfaces add the rest, and they are derived from the manager rather than read off the
    agent because that class attaches them on its first run, not at construction; they belong here
    because a skill script is an unsandboxed subprocess, which is precisely the kind of call somebody
    gates. ``activate_skill`` comes with them and only with them, since a ``SkillAgent`` with no skills
    builds no skills server at all.

    Two of those three have no toolset, so :data:`RESERVED_GATE_NAMESPACE` is the prefix they get.
    ``activate_skill`` needs the reservation for a second reason: ``LiveState.skill_tools`` prepends it
    to *every* skill's tool list, so leaving it attributed per skill would let a gate on one skill hold
    back the entry point to all of them. It is discarded from each skill here and attributed only to the
    reserved prefix. A skill's own scripts stay under the skill, which is also its toolset name.

    The reserved key is always present, even when it is empty (no delegates declared, no skills on
    disk), so a gate naming it reads as a capability that provides nothing rather than as a misspelling.
    """
    by_toolset = {name: set(tools) for name, tools in state.tools_by_toolset.items()}
    skills = state.skill_manager.skills.values()
    for skill in skills:
        by_toolset.setdefault(skill.name, set()).update(skill.script_tool_names())
    for tools in by_toolset.values():
        tools.discard(_ACTIVATE_SKILL)
    reserved = {_ACTIVATE_SKILL} if skills else set()
    owned = {tool for tools in by_toolset.values() for tool in tools}
    for fn in entry_agent.tools:
        name = getattr(fn, "__name__", None)
        if name and name not in owned:
            reserved.add(name)
    # An agent that can defer a toolset holds AIMU's loader tools, which no toolset provides, so a gate
    # on them is writable only under the reserved prefix.
    if any(loadable_toolsets(state.config, state.registry, name) for name in state.config.agents):
        reserved |= set(CORE_TOOL_NAMES)
    by_toolset[RESERVED_GATE_NAMESPACE] = reserved
    return by_toolset


def gate_entry_grammar_fault(entry: str) -> str:
    """What is malformed about ``entry``'s shape, or an empty string when it is well formed.

    Split out so an entry parked until its toolset is built is still checked at startup: the shape
    needs no tool names, and a malformed entry would otherwise fail a later build with a message about
    a missing tool while the real fault was discarded.
    """
    parts = entry.split(_GATE_SEPARATOR)
    if len(parts) > 2:
        return f"{entry!r} has more than one {_GATE_SEPARATOR!r}. {_GATE_GRAMMAR}"
    toolset, tool = (parts[0], _GATE_WILDCARD) if len(parts) == 1 else (parts[0], parts[1])
    if _GATE_WILDCARD in toolset or (tool != _GATE_WILDCARD and _GATE_WILDCARD in tool):
        return (
            f"{entry!r} uses {_GATE_WILDCARD!r} inside a name. It is legal only as the whole tool, as in "
            f"'compute.{_GATE_WILDCARD}', because a pattern matching fewer tools than its author meant "
            "is the same silent gap as a name matching none."
        )
    return ""


def built_toolsets(state: LiveState) -> set[str]:
    """The toolsets whose tool names already exist: the one answer to "is this toolset built yet?".

    Every toolset a ``build_tools`` call has recorded, plus every skill the entry agent's ``SkillAgent``
    holds. The second half matters because that agent's skills server is built by AIMU rather than
    through ``build_tools``, so no build ever records it, yet its tools exist from the first turn. Without
    it, a gate on such a skill would wait for a build whenever any agent could load the skill later, and
    the entry agent would run the skill's scripts ungated meanwhile.
    """
    return set(state.tools_by_toolset) | {skill.name for skill in state.skill_manager.skills.values()}


def pending_gate_entries(entries: Sequence[str], deferred: set[str], built: Collection[str]) -> set[str]:
    """The well-formed entries naming a deferred toolset that is not built yet (see :func:`built_toolsets`).

    These wait for the build instead of resolving at startup. A malformed entry is never parked, so it
    still reaches the resolver and fails startup with its own sentence.
    """
    return {
        entry
        for entry in entries
        if not gate_entry_grammar_fault(entry)
        and entry.split(_GATE_SEPARATOR)[0] in deferred
        and entry.split(_GATE_SEPARATOR)[0] not in built
    }


def certain_overlap(left: Collection[str], right: Collection[str]) -> list[str]:
    """What two lists of pending gate entries are sure to share once their toolset builds, as entries.

    Decided from the text alone, so a conflict between entries still waiting on a build fails startup
    rather than refusing every later build of that toolset. A whole toolset (``T`` or ``T.*``) holds
    whatever ``T.x`` names, and if ``x`` turns out not to exist, the ``T.x`` entry is dead and refuses
    the build on its own, so the pair can never build cleanly either way. Two different tools of one
    toolset are left alone: whether they collide is not something the text says.
    """
    shared: set[str] = set()
    for one in left:
        for other in right:
            toolset, tool = (one.split(_GATE_SEPARATOR) + [_GATE_WILDCARD])[:2]
            other_toolset, other_tool = (other.split(_GATE_SEPARATOR) + [_GATE_WILDCARD])[:2]
            if toolset != other_toolset:
                continue
            if tool == _GATE_WILDCARD:
                shared.add(toolset if other_tool == _GATE_WILDCARD else other)
            elif other_tool == _GATE_WILDCARD or tool == other_tool:
                shared.add(one)
    return sorted(shared)


def _resolve_gate_entry(entry: str, vocabulary: dict[str, set[str]], registry: Mapping[str, Toolset]) -> tuple:
    """One gate entry as ``(tool names, fault)``, exactly one of which is empty.

    Split out from :func:`resolve_gate_entries` so each way an entry can match nothing gets its own
    sentence naming the edit to make. A near-miss is worth more than a rejection here: the reader
    wrote a name meaning to hold a tool back, and the difference between the name they wrote and the
    one that works is the whole content of the error.
    """
    grammar_fault = gate_entry_grammar_fault(entry)
    if grammar_fault:
        return set(), grammar_fault
    parts = entry.split(_GATE_SEPARATOR)
    toolset, tool = (parts[0], _GATE_WILDCARD) if len(parts) == 1 else (parts[0], parts[1])
    if toolset not in vocabulary:
        if len(parts) == 1:
            owners = sorted(name for name, tools in vocabulary.items() if toolset in tools)
            if owners:
                forms = " or ".join(repr(f"{name}{_GATE_SEPARATOR}{toolset}") for name in owners)
                return set(), f"{entry!r} is a tool name with no toolset in front of it: write {forms}"
        if toolset in registry:
            return set(), (
                f"{entry!r} names the {toolset!r} toolset, which no agent declares, so it builds no "
                f"tools and holds nothing back. Add {toolset!r} to an [agents.<name>.tools].available list, or "
                "drop the gate."
            )
        close = difflib.get_close_matches(toolset, sorted(set(vocabulary) | set(registry)), n=2)
        hint = f" (did you mean {' or '.join(repr(match) for match in close)}?)" if close else ""
        return set(), f"{entry!r} names no toolset{hint}. {_GATE_GRAMMAR}"
    provided = vocabulary[toolset]
    if not provided:
        return set(), (
            f"{entry!r} names the {toolset!r} toolset, which provides no tools, so there is nothing "
            "under it to hold back."
        )
    if tool == _GATE_WILDCARD:
        return set(provided), ""
    if tool not in provided:
        elsewhere = sorted(name for name, tools in vocabulary.items() if tool in tools)
        if elsewhere:
            forms = " or ".join(repr(f"{name}{_GATE_SEPARATOR}{tool}") for name in elsewhere)
            return set(), f"{entry!r} is filed under the wrong toolset: {tool!r} comes from {forms}"
        close = difflib.get_close_matches(tool, sorted(provided), n=2)
        hint = f" (did you mean {' or '.join(repr(match) for match in close)}?)" if close else ""
        return set(), f"{entry!r} names no tool the {toolset!r} toolset provides{hint}"
    return {tool}, ""


def resolve_gate_entries(
    entries: Sequence[str],
    vocabulary: dict[str, set[str]],
    registry: Mapping[str, Toolset],
    *,
    setting: str,
    effect: str,
    remedy: str,
) -> frozenset[str]:
    """The tool names ``entries`` resolve to, rejecting any entry that would match nothing.

    Shared by every setting written in the ``<toolset>``/``<toolset>.<tool>`` vocabulary, so a
    misspelling in one of them gets the same "did you mean" as a misspelling in another. ``effect`` and
    ``remedy`` are the caller's own words, because what a dead entry does and what it costs both differ:
    a dead ``confirm`` entry gates nothing, and a tool with full machine access then runs
    unprompted, while a dead ``review`` entry reviews nothing, and a prompt somebody meant to stop
    receiving still arrives.
    """
    names: set[str] = set()
    faults: list[str] = []
    for entry in entries:
        resolved, fault = _resolve_gate_entry(entry, vocabulary, registry)
        if fault:
            faults.append(fault)
        else:
            names.update(resolved)
    if faults:
        noun = "entry" if len(faults) == 1 else "entries"
        raise ConfigError(f"{setting} has {len(faults)} {noun} that would {effect}: {'; '.join(faults)}. {remedy}")
    return frozenset(names)


def resolve_built_entries(
    entries: Collection[str],
    pending: Collection[str],
    toolset: str,
    tool_names: Collection[str],
    *,
    setting: str,
) -> tuple[set[str], set[str]]:
    """What the entries naming ``toolset`` match in this build of it, and which of ``pending`` still wait.

    Every entry naming the toolset is matched again, not only the pending ones, because a toolset can
    offer more on a later build than it did when an entry first resolved: a skill gains a script
    mid-session, and ``"weekly-digest"`` has to hold the new script back before any agent is handed it.
    The caller adds what this returns to the names it already holds, so a gate never loses one.

    An entry that matches nothing here refuses the build only while it is pending. One that resolved
    earlier was proven live by that build, so a later build lacking its tool has nothing to report.
    ``activate_skill`` is left out of the build's tools for the reason :func:`gateable_tools` gives:
    every skill's build carries it, and a gate on one skill must not hold back the loader for all.
    """
    vocabulary = {toolset: set(tool_names) - {_ACTIVATE_SKILL}}
    names: set[str] = set()
    waiting = set(pending)
    for entry in sorted(set(entries) | waiting):
        if entry.split(_GATE_SEPARATOR)[0] != toolset:
            continue
        resolved, fault = _resolve_gate_entry(entry, vocabulary, {})
        if fault and entry in waiting:
            raise ConfigError(
                f"{setting} names {entry!r}, but the {toolset!r} toolset, now built, provides no such tool. "
                f"Fix the entry; until then {toolset!r} cannot be loaded. {fault}."
            )
        names |= resolved
        waiting.discard(entry)
    return names, waiting
