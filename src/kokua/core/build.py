"""Builder functions that assemble an Assistant's parts: the model client and the per-conversation agent.

Split out of ``Assistant.create()`` so it reads as a short orchestrator and the wiring is testable
in isolation. These are free functions with no ``Assistant`` coupling: they take config/state and
return the built pieces. Runtime-settings application stays in ``assistant.py`` (it is shared
with ``apply_settings``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Sequence

from aimu import aio
from aimu.tools import ToolGroup
from aimu.tools.groups import CORE_TOOL_NAMES

from kokua.config.schema import AssistantConfig
from kokua.core.transcripts import SPAWN_SUBAGENT_TOOL_NAME, tool_results_by_call_id
from kokua.registry.context import LiveState, ToolsetContext
from kokua.registry.registry import Toolset, build_tools, select


logger = logging.getLogger(__name__)


class ModelClientError(RuntimeError):
    """The model client could not be built: no model resolved (no `AIMU_LANGUAGE_MODEL`, no
    running local provider) or an invalid model string. Carries AIMU's actionable message so a
    front end can present it instead of a traceback."""


def resolve_system_message(
    config: AssistantConfig, agent_name: str, toolsets: Sequence[Toolset], deferred: Sequence[Toolset] = ()
) -> str:
    """One agent's system prompt: its declared opener plus the guidance its toolsets and delegation earn.

    Shared by every path that builds a client for an agent, so each one gives it the same message the
    agent's toolsets and delegation earn rather than recomputing a possibly different one. ``toolsets``
    are the resident ones; ``deferred`` are named in the prompt and bring their guidance when loaded.
    """
    # Imported here, not at module level: kokua.core.agents pulls in kokua.toolsets.core, which pulls
    # in kokua.core.transcripts -- a submodule of this package -- and importing it triggers kokua/core/__init__
    # to run, which imports this module. A top-level import here would close that cycle.
    from kokua.core.agents import assemble_system_message

    return assemble_system_message(config, agent_name, toolsets, deferred)


def build_model_client(config: AssistantConfig, system_message: str, agent_name: str):
    """Build ``agent_name``'s model client, carrying ``system_message``.

    The model is ``config.model_for(agent_name)``: the agent's own declaration, else the
    ``[assistant].model`` default. The caller assembles the message (see ``resolve_system_message``)
    and passes it in, rather than this function computing its own, so every path that builds a client
    for an agent gives it the same message that agent was wired with. Raises ``ModelClientError``
    (carrying AIMU's message) instead of the raw ValueError/TypeError so a front end can present it
    rather than a traceback.

    Generation parameters come from ``config.generation_for(agent_name)`` and are assigned to the
    client, not the agent, which is why an injected client is left alone in ``wire_agent`` while
    ``thinking`` is applied there regardless: reasoning effort is a field on the agent, and these are a
    standing property of the client a caller may have built itself.
    """
    try:
        client = aio.client(config.model_for(agent_name), system=system_message)
    except (ValueError, TypeError) as e:
        raise ModelClientError(str(e)) from e
    # Only when something is declared, and only the keys that are: this is AIMU's third precedence
    # tier, above the model card, so writing an empty or defaulted dict would shadow a card's own
    # tuned sampling profile. See AssistantConfig.generation_for.
    generation = config.generation_for(agent_name)
    if generation:
        client.default_generate_kwargs = generation
    return client


def validate_model_string(model: str) -> None:
    """Raise ``ModelClientError`` unless this process could build a client for ``model``.

    Answered by building a throwaway client rather than by parsing the string, so a pass here means the
    same call in :func:`build_model_client` will succeed: the check is the thing it predicts, including
    the parts of it a parser cannot see (whether the provider's extra is installed at all). AIMU's async
    factory refuses the in-process providers outright rather than loading weights, and every other
    provider constructs a transport and makes no request, so this stays cheap and offline.

    Its caller is ``update_config`` writing ``[assistant].model``, which is startup-only: nothing applies
    it live, so without this an unresolvable string is persisted and surfaces only as a Kokua that will
    not start. The client is discarded; this reports, it does not build.
    """
    try:
        aio.client(model)
    except (ValueError, TypeError) as e:
        raise ModelClientError(str(e)) from e


def model_label(config: AssistantConfig, agent_name: str) -> str:
    """The model ``agent_name`` runs on, as a string for a person to read or a record to store.

    Always the string the config resolved, which is the form the user recognizes and could type back
    into ``config.toml``. This used to take the live client and fall back to it when nothing was
    declared anywhere, because ``model_for`` answered None in that case and the built client was the
    only place an answer existed. It is not a place an answer exists: a client reports a resolved
    ``Model`` enum, which renders as ``OllamaModel.QWEN_3_8_27B`` and cannot show the endpoint a
    default may carry. ``model_for`` is total now, so there is nothing left to fall back to.
    """
    return str(config.model_for(agent_name))


def entry_agent_system_message(config: AssistantConfig, state: LiveState) -> str:
    """The assembled system message for the entry agent -- the one every client Kokua builds directly needs.

    Every agent Kokua constructs directly (the initial build and every later per-conversation client)
    IS the entry agent, ``config.entry_agent``; only a spawned worker differs,
    and it gets its own message from ``build_agent_specs``. Reads ``config.agents[config.entry_agent]``
    rather than a hardcoded toolset list, so a renamed entry agent gets the message its own
    ``[agents.*]`` table declares rather than one assembled for whichever agent used to hold that role.
    Centralized here so ``Assistant.create``'s per-conversation client factory resolves its toolsets in
    one place, instead of duplicating ``select`` plus ``resolve_system_message`` and risking the copies
    drifting apart.
    """
    name = config.entry_agent
    from kokua.core.agents import available_for, split_by_residency, without_skill_names

    # Skills are held back exactly as `wire_agent` holds them back: the entry agent is a SkillAgent, which
    # already advertises them, so naming them here would offer to load what is not a toolset to load.
    resolvable = without_skill_names(available_for(config, state.registry, name), state.registry)
    toolsets = select(resolvable, state.registry, agent=name, entry_point=name)
    resident, deferred = split_by_residency(config, state.registry, name, toolsets)
    return resolve_system_message(config, name, resident, deferred)


def wire_agent(
    config: AssistantConfig,
    state: LiveState,
    agent_name: str,
    *,
    client=None,
    preloaded: Sequence[str] = (),
) -> aio.SkillAgent:
    """Build one fully-wired agent from its ``[agents.*]`` declaration: its own toolsets, the approval
    gate, and its delegate.

    Every agent is wired identically from its declaration, so a conversation's agent cannot differ from
    a sibling's by accident. Toolsets are selected once and the same list feeds both the system message
    and the built tools, so the two can never resolve a different toolset for the same declared names.

    ``client`` lets a caller that already has one (``make_agent_builder``, and tests that inject a mock
    client so the suite runs without a model) skip resolving a second, unused one from ``config``;
    omitting it resolves the model straight from ``config``, with the system message assembled here. An
    injected client keeps the system message its own caller built it with, except when ``preloaded``
    restores a toolset: then the message is rewritten here, because only this function knows which
    preloads built, and a restored toolset's guidance has to join the prompt and its name leave the line
    naming what is still loadable.

    ``thinking`` is set from the config even when a ``client`` is injected, unlike the system message:
    reasoning effort is a field on the *agent*, applied to every turn of a run, so there is nothing for
    a caller-built client to already carry.

    Declared *skill* names are held back from toolset resolution here, because this agent is a
    ``SkillAgent`` and AIMU already gives it their catalogue and script tools from
    ``state.skill_manager``. Resolving them as toolsets too would append the same script tools (harmless,
    deduplicated by name) and the same catalogue entries a second time in the prompt (not harmless). A
    spawned worker is a plain agent with no such machinery, so for it the registry is the only route and
    ``build_agent_specs`` resolves skill names like any other name.

    ``script_env`` is handed over for the same reason, in the opposite direction: a ``SkillAgent`` builds
    its own skills server, so the ``env`` ``LiveState.skill_tools`` passes when it builds one for a
    spawned worker never reaches this agent's scripts. Omitting it raises nothing, it just leaves the
    entry agent's scripts unable to see the ``[email]`` settings or the downloads folder, which each
    script reports as being unconfigured.

    ``preloaded`` names toolsets the conversation loaded in an earlier life of this agent. Each one still
    deferred is built here and joins the resident tools, in the order given, so a rebuilt agent holds what
    the model had loaded. The loads are not replayed through the agent because AIMU's load is async and
    this function is not; building them at wiring reaches the same tools, though not the same order: a
    preloaded toolset's tools join the agent's own list, so they sit ahead of ``spawn_subagent``, any skill
    tools, and the two loaders, where a model-driven load appends after all of them.
    They go through ``build_tools`` like every other build, so the approval entries waiting on a toolset
    resolve before any of its tools can be called. One that fails to build (a refused approval entry, an
    MCP server still down) is logged and left deferred, so the model can try loading it again and read the
    real error, and a name that is no longer deferred is ignored.
    """
    # Imported here, not at module level: see the comment in resolve_system_message -- the same cycle
    # runs through kokua.core.agents.
    from kokua.core.agents import (
        available_for,
        split_by_residency,
        without_core_tool_collisions,
        without_skill_names,
    )

    resolvable = without_skill_names(available_for(config, state.registry, agent_name), state.registry)
    toolsets = select(resolvable, state.registry, agent=agent_name, entry_point=config.entry_agent)
    resident, deferred = split_by_residency(config, state.registry, agent_name, toolsets)
    resolved_client = client
    if resolved_client is None:
        resolved_client = build_model_client(
            config, resolve_system_message(config, agent_name, resident, deferred), agent_name
        )

    deferred_by_name = {toolset.name: toolset for toolset in deferred}
    loaded = [deferred_by_name[name] for name in dict.fromkeys(preloaded) if name in deferred_by_name]

    def context() -> ToolsetContext:
        # Reads `agent` when called, which is after the assignment below: the groups are handed to the
        # agent's constructor, and a toolset such as `skills` needs the live agent when it is built.
        return ToolsetContext(state=state, agent=agent, agent_name=agent_name)

    while True:
        agent = aio.SkillAgent(
            resolved_client,
            tools=[],
            skill_manager=state.skill_manager,
            script_env=state.script_env(),
            name=agent_name,
            concurrent_tool_calls=config.concurrent_tools,
            thinking=config.thinking_for(agent_name),
            max_iterations=config.max_iterations_for(agent_name),
            tool_groups=tool_groups_for(
                config,
                state,
                agent_name,
                [*resident, *loaded],
                [toolset for toolset in deferred if toolset not in loaded],
                context,
            ),
        )
        agent.tools = without_core_tool_collisions(config, state.registry, agent_name, build_tools(resident, context()))
        failed = _append_preloads(config, state, agent, loaded, context)
        if not failed:
            break
        # A group's residency is fixed when the agent is constructed, so a failure means constructing again
        # with it deferred. What built is built again too, since a toolset like `skills` holds its agent.
        loaded = [toolset for toolset in loaded if toolset not in failed]

    if loaded:
        # Overwrites an injected client's message too (see the docstring): the restored toolsets bring
        # their guidance and leave the line naming what is still loadable.
        resolved_client.system_message = resolve_system_message(
            config,
            agent_name,
            [*resident, *loaded],
            [toolset for toolset in deferred if toolset not in loaded],
        )
    agent.tool_approval = state.gate_for(agent_name)
    rebuild_delegation_tool(agent, state)
    return agent


def _append_preloads(
    config: AssistantConfig,
    state: LiveState,
    agent: aio.SkillAgent,
    preloads: Sequence[Toolset],
    context: Callable[[], ToolsetContext],
) -> list[Toolset]:
    """Build each of ``preloads`` and append its tools to ``agent.tools``; return the ones that failed.

    A failure is logged rather than alerted (see ``build_deferred``) and is the caller's to act on: the
    agent was constructed with these toolsets marked resident, so leaving one out means constructing it
    again.
    """
    from kokua.core.agents import core_tool_names_for_agent

    agent_name = agent.name or config.entry_agent
    taken = (
        {getattr(fn, "__name__", None) for fn in agent.tools}
        | set(CORE_TOOL_NAMES)
        | core_tool_names_for_agent(config, state.registry, agent_name)
    )
    failed = []
    for toolset in preloads:
        try:
            built = build_deferred(config, state, toolset, context)
        except Exception as error:
            logger.warning("Could not restore the %r toolset for %s: %s", toolset.name, agent.name, error)
            failed.append(toolset)
            continue
        # A name the agent already holds is skipped, as AIMU does for a model-driven load: providers
        # reject a request carrying two tools of one name.
        for fn in built:
            name = getattr(fn, "__name__", None)
            if name in taken:
                logger.warning(
                    "Restoring %r for %s skipped %r: the agent already has a tool by that name",
                    toolset.name,
                    agent.name,
                    name,
                )
                continue
            taken.add(name)
            agent.tools.append(fn)
    return failed


def tool_groups_for(
    config: AssistantConfig,
    state: LiveState,
    agent_name: str,
    resident: Sequence[Toolset],
    deferred: Sequence[Toolset],
    context: Callable[[], ToolsetContext],
) -> list[ToolGroup]:
    """The tool groups one agent carries: its resident toolsets listed, its deferred ones loadable.

    A resident group has no ``build``, since its tools are already in the agent's own list and it exists
    so ``available_tools`` shows the whole picture. A deferred group's ``build`` is ``build_tools`` over
    that one toolset, the same call a resident toolset goes through at wiring, which is what keeps tool
    recording and the approval gate identical however late a toolset arrives. Its guidance rides the
    group and reaches the model when it loads, instead of sitting in the prompt from the first turn.

    A build failure is raised again after the user is told: AIMU turns the exception into the text the
    model reads, and nothing else would tell the person that a capability they expected did not arrive.

    ``context`` is a callable rather than a context because the entry agent's context holds the agent, and
    the agent is constructed with these groups: the callable is only called at load time, when the agent
    exists, so no context is ever built half empty or changed after the fact.

    A deferred MCP server that is not connected refuses to build. Resident, the same server builds empty
    (a reconnect reaches the next rebuild), but a load is recorded as done, so an empty one would leave
    the group unloadable for the conversation even after the server returns.
    """
    # Imported here, not at module level: see the comment in resolve_system_message.
    from kokua.core.agents import approval_note

    confirm, review, _ = config.approval_entries_for(agent_name)

    def group(toolset: Toolset, *, is_deferred: bool) -> ToolGroup:
        def build(toolset: Toolset = toolset) -> list:
            try:
                return build_deferred(config, state, toolset, context)
            except Exception as error:
                if state.alert_soon is not None:
                    state.alert_soon(f"Loading {toolset.name} for {agent_name} failed: {error}")
                raise

        return ToolGroup(
            toolset.name,
            toolset.description,
            build=build if is_deferred else None,
            guidance=toolset.guidance if is_deferred else "",
            note=approval_note(confirm, review, toolset.name),
        )

    return [group(toolset, is_deferred=False) for toolset in resident] + [
        group(toolset, is_deferred=True) for toolset in deferred
    ]


def build_deferred(
    config: AssistantConfig, state: LiveState, toolset: Toolset, context: Callable[[], ToolsetContext]
) -> list:
    """The tools of one deferred toolset, through the build every late arrival shares.

    Whether the model asked for it with ``load_tools`` or a rebuilt agent is restoring it, the
    connected-server check and the approval hook are the same. Who is told of a failure is the caller's
    call: a load the model asked for alerts the user, while a restore stays in the log, since a rebuild
    happens on restart or when a conversation is switched back to and nobody asked for anything.
    """
    server_urls = {server.name: server.url for server in config.mcp_servers}
    url = server_urls.get(toolset.name)
    if url is not None and not any(getattr(c, "url", None) == url for c in state.connections):
        raise ConnectionError(f"the MCP server {toolset.name!r} ({url}) is not connected")
    return build_tools([toolset], context())


def loaded_toolsets_in(messages: list[dict]) -> list[str]:
    """The toolsets a conversation's model loaded, by name, in the order each first loaded.

    Only a load that succeeded counts, judged by the result stored for its call, which AIMU opens with
    ``Loaded '<name>':`` and nothing else does. The call alone is not evidence: AIMU stores it whether or
    not it ran, so a load the approval gate refused (by the user, or auto-denied in a turn nobody was
    watching) would otherwise come back at the next restart, eviction, or truncation, and a denied load
    has to stay denied. A load that failed, named nothing loadable, or has no result (a turn cancelled
    mid-call) is left out as well, so the model can load it again and read whatever happens then. A repeat
    answered "already loaded" or "resident" is left out too, and loses nothing: the load that made it so
    sits earlier in the same transcript, since truncation only ever removes a conversation's tail.

    Read from the stored transcript rather than kept separately, so there is no second record to drift
    from it. A call's arguments are a dict or a JSON string depending on the provider; one that decodes to
    anything else, or has no string ``name``, is skipped.
    """
    results = tool_results_by_call_id(messages)
    names: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != "load_tools":
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    continue
            name = arguments.get("name") if isinstance(arguments, dict) else None
            name = name.strip() if isinstance(name, str) else None
            if not name or name in names:
                continue
            if results.get(call.get("id"), "").startswith(f"Loaded {name!r}:"):
                names.append(name)
    return names


def make_agent_builder(
    config: AssistantConfig,
    state: LiveState,
    *,
    client_factory: Callable[[str], object],
    store,
    images_path: Path,
) -> Callable[[str], aio.SkillAgent]:
    """Return a builder that constructs and restores a per-conversation agent on demand.

    Each call to ``client_factory`` must return a fresh model client: agents share no client, since
    a shared client's ``.messages`` would defeat per-conversation isolation.
    """
    from kokua.core.messages import expand_message_images

    # Assigned here, once, rather than by the caller: the mcp toolset reads this off the context
    # instead of importing this module directly (this module reaches toolsets.mcp through
    # toolsets.agents, so that import would be circular), and every agent this builder produces must
    # share the one callback.
    state.refresh_workers = lambda agent: rebuild_delegation_tool(agent, state)
    state.refresh_skills = lambda *, tools_changed: refresh_skills(state, tools_changed=tools_changed)

    def build(conversation_id: str) -> aio.SkillAgent:
        session = store.get(conversation_id)
        preloaded = loaded_toolsets_in(session.messages) if session is not None else []
        agent = wire_agent(
            config, state, config.entry_agent, client=client_factory(conversation_id), preloaded=preloaded
        )
        if session is not None and session.messages:
            agent.restore(expand_message_images(session.messages, images_path))
        return agent

    return build


def rebuild_delegation_tool(agent: aio.SkillAgent, state: LiveState) -> None:
    """Replace an agent's delegate with one built from the CURRENT connections.

    A worker's toolset is snapshotted when the delegate is built, so a runtime MCP add or remove has to
    rebuild it for the change to reach that worker. The new delegate is built before the old one is
    removed, so a build that raises leaves the agent delegating as it did rather than not at all.
    """
    # Imported here, not at module level: see the comment in resolve_system_message -- the same cycle
    # runs through kokua.core.agents.
    from kokua.core.agents import make_delegation_tool

    tool = make_delegation_tool(agent, state.config, state)
    agent.tools[:] = [t for t in agent.tools if getattr(t, "__name__", None) != SPAWN_SUBAGENT_TOOL_NAME]
    if tool is not None:
        agent.tools.append(tool)


def refresh_skills(state: LiveState, *, tools_changed: bool) -> None:
    """Bring the skills on disk into the registry, so a skill written mid-session reaches its next readers.

    Who that reaches follows from who reads the registry when. An ``"all"`` worker's spec is resolved when
    its delegator's ``spawn_subagent`` is built, so every live agent's delegate is rebuilt here and the new
    skill arrives on that worker's next spawn. ``compose_subagent`` and ``list_capabilities`` read the
    registry on every call, so they see it on their next call with nothing rebuilt. A worker declaring a
    list is unchanged, since a list names its skills; the one thing it does pick up is new scripts on a
    skill it already names, because ``skill_tools`` is rebuilt for everyone. A conversation whose agent
    is not built yet reads the new registry when it is.

    ``tools_changed`` says whether the write could change which tools a skill offers (a new skill, a new
    script). Only then is ``skill_tools`` rebuilt, since each rebuild retires a skills server that stays
    open until the process ends; a prose edit changes descriptions and guidance, which live in the registry.
    Only then, too, is each skill's tool list reported to ``state.toolset_built`` as a build, so approval
    entries naming a skill grow to its new scripts before a single delegate is rebuilt.

    A collision (a skill on disk claiming another toolset's name) raises ``ToolsetError`` before anything
    is swapped, so the previous registry stays in force. An approval hook refusing a skill's new tools
    raises ``ConfigError`` before any delegate is rebuilt, so no worker spec is handed them.
    """
    # Imported here, not at module level: see the comment in resolve_system_message -- the same cycle
    # runs through kokua.core.agents.
    from kokua.core.agents import skill_toolsets, with_skill_toolsets

    registry = with_skill_toolsets(state.registry, skill_toolsets(state.config))
    # A plain assignment, with no lock, and safe beside readers already running. The only builds off the
    # event loop thread are deferred groups loading in a worker thread, and each holds the `Toolset` it
    # was wired with rather than looking its name up again. Everything that does look names up (a spec
    # build, a compose, a catalogue listing) is synchronous on the event loop, as this function is, so it
    # runs wholly before or wholly after this line and sees one registry, old or new, never a mixture.
    state.registry = registry
    if tools_changed:
        state.refresh_skill_tools()
        # Reported as builds before any delegate is rebuilt, because a rebuilt delegate hands workers the
        # new scripts and the entry agent's SkillAgent already holds them: a gate naming a skill has to
        # cover a new script before anything can call it (see kokua.core.approval).
        if state.toolset_built is not None:
            for skill in state.all_skills.skills.values():
                state.toolset_built(skill.name, frozenset(skill.script_tool_names()))
    if state.for_each_agent is not None and state.refresh_workers is not None:
        state.for_each_agent(state.refresh_workers)
