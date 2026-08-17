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
from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.registry import Toolset, build_tools, select


class ModelClientError(RuntimeError):
    """The model client could not be built: no model resolved (no `AIMU_LANGUAGE_MODEL`, no
    running local provider) or an invalid model string. Carries AIMU's actionable message so a
    front end can present it instead of a traceback."""


def resolve_system_message(config: AssistantConfig, agent_name: str, toolsets: Sequence[Toolset]) -> str:
    """One agent's system prompt: its declared opener plus the guidance its toolsets and delegation earn.

    Shared by the initial build and a runtime model switch, so a switch reuses exactly the message the
    agent was built with rather than recomputing a possibly different one.
    """
    # Imported here, not at module level: kokua.toolsets.agents pulls in kokua.toolsets.core, which pulls
    # in kokua.core.transcripts -- a submodule of this package -- and importing it triggers kokua/core/__init__
    # to run, which imports this module. A top-level import here would close that cycle.
    from kokua.toolsets.agents import assemble_system_message

    return assemble_system_message(config, agent_name, toolsets)


def build_model_client(config: AssistantConfig, system_message: str):
    """Build the model client for ``config.model``, carrying ``system_message``.

    The caller assembles the message (see ``resolve_system_message``) and passes it in, rather than this
    function computing its own, so a runtime model switch and the initial build always give the client
    the same message the agent was wired with. Raises ``ModelClientError`` (carrying AIMU's message)
    instead of the raw ValueError/TypeError so a front end can present it rather than a traceback.
    """
    try:
        return aio.client(config.model, system=system_message)
    except (ValueError, TypeError) as e:
        raise ModelClientError(str(e)) from e


def entry_agent_system_message(config: AssistantConfig, state: LiveState) -> str:
    """The assembled system message for the entry agent -- the one every client Kokua builds directly needs.

    Every agent Kokua constructs directly (the initial build, a runtime model switch, and every later
    per-conversation client) IS the entry agent, ``config.entry_agent``; only a spawned worker differs,
    and it gets its own message from ``build_agent_specs``. Reads ``config.agents[config.entry_agent]``
    rather than a hardcoded toolset list, so a renamed entry agent gets the message its own
    ``[agents.*]`` table declares rather than one assembled for whichever agent used to hold that role.
    Centralized here so the callers that build a client for the entry agent --
    ``SettingsApplier.switch_model`` and ``Assistant.create``'s per-conversation client factory --
    resolve its toolsets exactly once, in one place, instead of duplicating ``select`` plus
    ``resolve_system_message`` and risking the copies drifting apart.
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

    ``client`` lets a caller that already has one (``make_agent_builder``, whose factory layers
    per-conversation generation kwargs onto it, and tests that inject a mock client so the suite runs
    without a model) skip resolving a second, unused one from ``config``; omitting it resolves the
    model straight from ``config``. Only that second path assembles and applies a system message: an
    injected client already carries whatever system its own caller built it with, so computing one here
    and overwriting it would defeat the injection.

    Declared *skill* names are held back from toolset resolution here, because this agent is a
    ``SkillAgent`` and AIMU already gives it their catalogue and script tools from
    ``state.skill_manager``. Resolving them as toolsets too would append the same script tools (harmless,
    deduplicated by name) and the same catalogue entries a second time in the prompt (not harmless). A
    spawned worker is a plain agent with no such machinery, so for it the registry is the only route and
    ``build_agent_specs`` resolves skill names like any other name.
    """
    # Imported here, not at module level: see the comment in resolve_system_message -- the same cycle
    # runs through kokua.toolsets.agents.
    from kokua.toolsets.agents import without_skill_names

    resolvable = without_skill_names(config.agents[agent_name].tools, state.registry)
    toolsets = select(resolvable, state.registry, agent=agent_name, entry_point=config.entry_agent)
    resolved_client = client
    if resolved_client is None:
        resolved_client = build_model_client(config, resolve_system_message(config, agent_name, toolsets))

    agent = aio.SkillAgent(
        resolved_client,
        tools=[],
        skill_manager=state.skill_manager,
        name=agent_name,
        concurrent_tool_calls=config.concurrent_tools,
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
    # runs through kokua.toolsets.agents.
    from kokua.toolsets.agents import make_delegation_tool

    agent.tools[:] = [t for t in agent.tools if getattr(t, "__name__", None) != SPAWN_SUBAGENT_TOOL_NAME]
    tool = make_delegation_tool(agent, state.config, state)
    if tool is not None:
        agent.tools.append(tool)
