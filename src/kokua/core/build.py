"""Builder functions that assemble an Assistant's parts: the model client and the per-conversation agent.

Split out of ``Assistant.create()`` so it reads as a short orchestrator and the wiring is testable
in isolation. These are free functions with no ``Assistant`` coupling: they take config/state and
return the built pieces. Runtime-settings application stays in ``assistant.py`` (it is shared
with ``apply_settings``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from aimu import aio

from kokua.channels.web import SPAWN_SUBAGENT_TOOL_NAME
from kokua.config.schema import AssistantConfig
from kokua.registry.context import LiveState, ToolsetContext
from kokua.registry.registry import Toolset, build_tools, select


class ModelClientError(RuntimeError):
    """The model client could not be built: no model resolved (no `AIMU_LANGUAGE_MODEL`, no
    running local provider) or an invalid model string. Carries AIMU's actionable message so a
    front end can present it instead of a traceback."""


def resolve_system_message(config: AssistantConfig, agent_name: str, toolsets: Sequence[Toolset]) -> str:
    """One agent's system prompt: its declared opener plus the guidance its toolsets and delegation earn.

    Shared by every path that builds a client for an agent, so each one gives it the same message the
    agent's toolsets and delegation earn rather than recomputing a possibly different one.
    """
    # Imported here, not at module level: kokua.core.agents pulls in kokua.toolsets.core, which pulls
    # in kokua.core.transcripts -- a submodule of this package -- and importing it triggers kokua/core/__init__
    # to run, which imports this module. A top-level import here would close that cycle.
    from kokua.core.agents import assemble_system_message

    return assemble_system_message(config, agent_name, toolsets)


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
    toolsets = select(config.agents[name].tools, state.registry, agent=name, entry_point=name)
    return resolve_system_message(config, name, toolsets)


def wire_agent(config: AssistantConfig, state: LiveState, agent_name: str, *, client=None) -> aio.SkillAgent:
    """Build one fully-wired agent from its ``[agents.*]`` declaration: its own toolsets, the approval
    gate, and its delegate.

    Every agent is wired identically from its declaration, so a conversation's agent cannot differ from
    a sibling's by accident. Toolsets are selected once and the same list feeds both the system message
    and the built tools, so the two can never resolve a different toolset for the same declared names.

    ``client`` lets a caller that already has one (``make_agent_builder``, and tests that inject a mock
    client so the suite runs without a model) skip resolving a second, unused one from ``config``;
    omitting it resolves the
    model straight from ``config``. Only that second path assembles and applies a system message: an
    injected client already carries whatever system its own caller built it with, so computing one here
    and overwriting it would defeat the injection.

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
    """
    # Imported here, not at module level: see the comment in resolve_system_message -- the same cycle
    # runs through kokua.core.agents.
    from kokua.core.agents import without_skill_names

    resolvable = without_skill_names(config.agents[agent_name].tools, state.registry)
    toolsets = select(resolvable, state.registry, agent=agent_name, entry_point=config.entry_agent)
    resolved_client = client
    if resolved_client is None:
        resolved_client = build_model_client(config, resolve_system_message(config, agent_name, toolsets), agent_name)

    agent = aio.SkillAgent(
        resolved_client,
        tools=[],
        skill_manager=state.skill_manager,
        script_env=state.script_env(),
        name=agent_name,
        concurrent_tool_calls=config.concurrent_tools,
        thinking=config.thinking_for(agent_name),
    )
    agent.tools = build_tools(toolsets, ToolsetContext(state=state, agent=agent))
    agent.tool_approval = state.tool_approval
    rebuild_delegation_tool(agent, state)
    return agent


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

    # Assigned here, once, rather than by the caller: the mcp-admin toolset reads this off the context
    # instead of importing this module directly (this module reaches toolsets.mcp_admin through
    # toolsets.agents, so that import would be circular), and every agent this builder produces must
    # share the one callback.
    state.refresh_workers = lambda agent: rebuild_delegation_tool(agent, state)

    def build(conversation_id: str) -> aio.SkillAgent:
        agent = wire_agent(config, state, config.entry_agent, client=client_factory(conversation_id))
        session = store.get(conversation_id)
        if session is not None and session.messages:
            agent.restore(expand_message_images(session.messages, images_path))
        return agent

    return build


def rebuild_delegation_tool(agent: aio.SkillAgent, state: LiveState) -> None:
    """Replace an agent's delegate with one built from the CURRENT connections.

    A worker's toolset is snapshotted when the delegate is built, so a runtime MCP add or remove has to
    rebuild it for the change to reach that worker.
    """
    # Imported here, not at module level: see the comment in resolve_system_message -- the same cycle
    # runs through kokua.core.agents.
    from kokua.core.agents import make_delegation_tool

    agent.tools[:] = [t for t in agent.tools if getattr(t, "__name__", None) != SPAWN_SUBAGENT_TOOL_NAME]
    tool = make_delegation_tool(agent, state.config, state)
    if tool is not None:
        agent.tools.append(tool)
