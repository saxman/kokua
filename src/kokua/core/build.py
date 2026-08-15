"""Builder functions that assemble an Assistant's parts: the model client and the per-conversation agent.

Split out of ``Assistant.create()`` so it reads as a short orchestrator and the wiring is testable
in isolation. These are free functions with no ``Assistant`` coupling: they take config/state and
return the built pieces. Runtime-settings application stays in ``assistant.py`` (it is shared
with ``apply_settings``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from aimu import aio
from aimu.aio.tools.builtin import make_async_subagent_tool

from kokua.channels.web import SPAWN_SUBAGENT_TOOL_NAME
from kokua.config.schema import MEMORY_GUIDANCE, SUPERVISOR_GUIDANCE, AssistantConfig
from kokua.toolsets.agents import agent_tools, build_registry
from kokua.toolsets.builtin import BUILTIN_TOOLSETS
from kokua.toolsets.context import LiveState

logger = logging.getLogger(__name__)


class ModelClientError(RuntimeError):
    """The model client could not be built: no model resolved (no `AIMU_LANGUAGE_MODEL`, no
    running local provider) or an invalid model string. Carries AIMU's actionable message so a
    front end can present it instead of a traceback."""


# The builtin toolsets that are not one of AIMU's selectable tool groups: the two persistent stores and
# skills authoring are each named directly by an agent (or, today, by `_ENTRY_POINT_TOOLSETS`), never by
# [tools].groups, so they are excluded from the group-name ceiling below.
_NON_GROUP_BUILTIN_TOOLSETS = frozenset({"memory", "documents", "skills"})


def _enabled_group_names(config: AssistantConfig) -> set[str]:
    """The AIMU tool-group names workers may draw from, validated.

    Also the one place ``[tools].groups`` is checked. Nothing else expands that list any more (the
    supervisor mounts no built-in groups, and a role's own ``groups`` are intersected with this set and
    otherwise ignored), so without an explicit check a typo like ``groups = ["complete"]`` would
    silently produce tool-less workers instead of failing at startup.
    """
    aimu_groups = {t.name for t in BUILTIN_TOOLSETS} - _NON_GROUP_BUILTIN_TOOLSETS
    for name in config.tools:
        if name not in aimu_groups and name not in ("all", "none"):
            valid = ", ".join(sorted(aimu_groups)) + ", all, none"
            raise ValueError(f"unknown tool group {name!r}; choose from: {valid}")
    if "all" in config.tools:
        return set(aimu_groups)
    return {g for g in config.tools if g != "none"}


def unreferenced_mcp_servers(config: AssistantConfig) -> list[str]:
    """Configured MCP servers that no sub-agent role names, and so reach no agent at all.

    The entry point agent mounts no MCP callables (see ``wire_agent``), so a server's tools travel only
    to the workers whose roles list it in ``mcp_servers``, by name or by raw URL. A server nobody names
    still connects and still spends its token on the handshake; it is simply unreachable, and nothing
    said so. Returns each one's name (or URL, when unnamed) for the startup warning.
    """
    referenced = {ref for role in config.subagent_roles.values() for ref in role.get("mcp_servers", [])}
    return [s.name or s.url for s in config.mcp_servers if not ({s.url, s.name} & referenced)]


def _build_subagent_agent_types(config: AssistantConfig, state: Optional[LiveState] = None) -> dict[str, dict]:
    """Build AIMU ``agent_types`` (worker specs) from ``config.subagent_roles``.

    Empty in, empty out: with no configured roles there is no worker menu, and ``add_subagent_tool``
    builds an untyped delegate instead.

    A role's worker toolset is the union, deduped by ``__name__``, of:
      - its ``groups`` (intersected with the assistant's enabled ``config.tools``) and its ``tool_packs``,
        both just toolset names now and so resolved together through ``agent_tools`` against the same
        registry a real agent draws from. A role is not itself an agent, so it is passed as its own
        label and never as the entry point; an unrecognized name is dropped rather than raised, which is
        the forgiving contract a typo'd group or pack has always had (unlike a real agent's declared
        toolsets, where an unknown name is a startup error);
      - the tools of any MCP servers it names in ``mcp_servers``, matched against the live
        ``state.connections`` by the server's configured ``name`` first, then its raw URL. MCP servers
        are per-connection, not named capabilities, so they stay outside the registry;
      - the ``time`` group, added to every role when it is globally enabled, because a worker that
        cannot tell the time is broken whatever its domain. This is the one addition a role does not
        ask for, and it still respects the global set rather than exceeding it.
    ``state`` defaults to a fresh one built from ``config`` (no live connections, the full registry),
    which is enough to resolve a role's built-in groups and tool-packs even before the MCP/plugin sources
    are wired. The role ``description`` becomes the first line of the built ``system_message`` (AIMU
    shows it in the tool's role menu).
    """
    state = state or LiveState(config=config, registry=build_registry(config))
    enabled = _enabled_group_names(config)
    url_by_name = {s.name: s.url for s in config.mcp_servers if getattr(s, "name", None)}
    callables_by_url = {getattr(c, "url", None): getattr(c, "callables", []) for c in state.connections}
    # Every role also gets the time group, on top of whatever its own `groups` name. A worker scoped to
    # compute or to a single tool-pack still has to resolve "by tomorrow morning", and a role that
    # forgot to ask for a clock produced a worker that silently could not tell the time. Gated on the
    # global set, so this still never grants a tool the user disabled in [tools].groups.
    ambient = ["time"] if "time" in enabled else []
    agent_types: dict[str, dict] = {}
    for name, role in config.subagent_roles.items():
        wanted = [g for g in role.get("groups", []) if g in enabled] + list(role.get("tool_packs", [])) + ambient
        toolset_names = [n for n in wanted if n in state.registry]
        tools = agent_tools(None, toolset_names, state, entry_point="", agent_name=name)
        seen = {getattr(fn, "__name__", None) for fn in tools}
        for ref in role.get("mcp_servers", []):
            for fn in callables_by_url.get(url_by_name.get(ref, ref), []):
                fname = getattr(fn, "__name__", None)
                if fname and fname not in seen:
                    seen.add(fname)
                    tools.append(fn)
        body = role.get("system_message", "")
        description = role.get("description", name)
        system_message = f"{description}\n\n{body}" if body else description
        agent_types[name] = {"system_message": system_message, "tools": tools}
    return agent_types


def resolve_system_message(config: AssistantConfig) -> str:
    """The system prompt for the model client: base message plus the memory guidance, when memory is on.

    Shared by the initial build and a runtime model switch. The supervisor guidance is unconditional:
    delegation is the only route to a domain tool, so there is no configuration under which the model
    should be told anything else.
    """
    return config.system_message + (MEMORY_GUIDANCE if config.memory else "") + SUPERVISOR_GUIDANCE


def build_model_client(config: AssistantConfig):
    """Build the model client for ``config.model``.

    The persisted model choice already lives in ``config.model`` (loaded from config.toml), so there is
    no separate override to apply. Raises ``ModelClientError`` (carrying AIMU's message) instead of the
    raw ValueError/TypeError so a front end can present it rather than a traceback.
    """
    try:
        return aio.client(config.model, system=resolve_system_message(config))
    except (ValueError, TypeError) as e:
        raise ModelClientError(str(e)) from e


# The entry point agent's capabilities, pending the [agents.*] config that replaces this list. Matches
# the set docs/explanation/architecture.md documents and tests/core/test_build.py pins.
_ENTRY_POINT_TOOLSETS = (
    "skills",
    "mcp-admin",
    "memory",
    "documents",
    "config",
    "time",
    "scheduling",
    "conversations",
)
# The two toolsets that back --no-memory: unlike every other name in _ENTRY_POINT_TOOLSETS, the built-in
# memory/documents toolsets have no config gate of their own (they build their tools unconditionally from
# whatever store LiveState hands them), so wire_agent is the one place that still has to check the flag.
_MEMORY_TOOLSETS = frozenset({"memory", "documents"})


def wire_agent(config: AssistantConfig, state: LiveState, agent_name: str, *, client=None) -> aio.SkillAgent:
    """Build one fully-wired agent: its declared toolsets, the approval gate, and its delegate.

    Every agent is wired identically from its declaration, so a conversation's agent cannot differ from
    a sibling's by accident. ``client`` lets a caller that already has one (``make_agent_builder``, which
    layers per-conversation generation kwargs onto it) skip resolving a second, unused one from
    ``config``; omitting it resolves the model straight from ``config``, for a caller with no client of
    its own.
    """
    agent = aio.SkillAgent(
        client if client is not None else build_model_client(config),
        tools=[],
        skill_manager=state.skill_manager,
        name=agent_name,
        concurrent_tool_calls=config.subagents_concurrent,
    )
    names = (
        _ENTRY_POINT_TOOLSETS if config.memory else tuple(n for n in _ENTRY_POINT_TOOLSETS if n not in _MEMORY_TOOLSETS)
    )
    agent.tools = agent_tools(agent, names, state, entry_point=agent_name, agent_name=agent_name)
    agent.tool_approval = state.tool_approval
    add_subagent_tool(agent, config, state)
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
    # instead of importing this module directly (this module imports mcp.tools, so that import would be
    # circular), and every agent this builder produces must share the one callback.
    state.refresh_workers = lambda agent: rebuild_delegation_tool(agent, state)

    def build(conversation_id: str) -> aio.SkillAgent:
        agent = wire_agent(config, state, "assistant", client=client_factory(conversation_id))
        session = store.get(conversation_id)
        if session is not None and session.messages:
            agent.restore(expand_message_images(session.messages, images_path))
        return agent

    return build


def add_subagent_tool(agent: aio.SkillAgent, config: AssistantConfig, state: LiveState) -> None:
    """Append the typed ``spawn_subagent(agent_type, task)`` delegate, the entry point's only route to a
    domain tool.

    Each spawn clones the active model and gets its role's scoped tool subset: built-in groups
    (intersected with ``config.tools``) plus any MCP servers and tool-packs the role names, resolved
    against ``state.connections`` and the toolset registry. The parent-only stateful toolsets (memory,
    skills, MCP management, config, scheduling, conversations) are deliberately withheld. Concurrent
    spawns overlap under the parent's ``concurrent_tool_calls``; the approval gate is forwarded so a
    sub-agent's gated-tool calls (e.g. execute_python) prompt via the parent rather than running
    unattended. ``state.observer`` is how a front end shows the spawn's work while it runs.

    ``config.subagent_roles`` is non-empty by the time this runs (``Assistant.create`` rejects an empty
    set), which also satisfies AIMU's rule that ``agent_types`` must not be an empty dict.
    """
    agent.tools.append(
        make_async_subagent_tool(
            agent.model_client.model,
            agent_types=_build_subagent_agent_types(config, state),
            tool_approval=state.tool_approval,
            observer=state.observer,
        )
    )


def rebuild_subagent_tool(agent: aio.SkillAgent, state: LiveState) -> None:
    """Replace an agent's ``spawn_subagent`` tool with a fresh one built from the CURRENT connections.

    Worker roles snapshot their toolset when ``spawn_subagent`` is built, so after a runtime MCP
    add/remove this re-resolves each role (picking up or dropping the changed server's tools)."""
    agent.tools[:] = [t for t in agent.tools if getattr(t, "__name__", None) != SPAWN_SUBAGENT_TOOL_NAME]
    add_subagent_tool(agent, state.config, state)


def rebuild_delegation_tool(agent: aio.SkillAgent, state: LiveState) -> None:
    """Replace an agent's delegate with one built from the CURRENT connections.

    A role snapshots its toolset when the delegate is built, so a runtime MCP add or remove has to
    rebuild it for the change to reach that role's workers.
    """
    rebuild_subagent_tool(agent, state)
