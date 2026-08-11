"""Builder functions that assemble an Assistant's parts: model client, memory, agent, and tools.

Split out of ``Assistant.create()`` so it reads as a short orchestrator and the wiring is testable
in isolation. These are free functions with no ``Assistant`` coupling: they take config/client/agent
and return the built pieces. Runtime-settings application stays in ``assistant.py`` (it is shared
with ``apply_settings``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from aimu import aio
from aimu.aio.tools.builtin import SubagentObserver, make_async_subagent_tool
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.skills import SkillManager, make_skill_authoring_tool, make_skill_script_tool
from aimu.tools import builtin
from aimu.tools.builtin import make_document_tools, make_memory_tools

from kokua.config.schema import MEMORY_GUIDANCE, SUPERVISOR_GUIDANCE, AssistantConfig
from kokua.channels.web import SPAWN_SUBAGENT_TOOL_NAME
from kokua.config.tools import make_config_tools
from kokua.mcp.servers import make_mcp_tools
from kokua.mcp.auth import Notify
from kokua.plugins import discover_tool_packs

logger = logging.getLogger(__name__)


class ModelClientError(RuntimeError):
    """The model client could not be built: no model resolved (no `AIMU_LANGUAGE_MODEL`, no
    running local provider) or an invalid model string. Carries AIMU's actionable message so a
    front end can present it instead of a traceback."""


# AIMU's built-in tool subgroups, selectable by name via the --tools flag / AssistantConfig.tools.
# The generative groups (image/audio/speech/transcription) need their AIMU_*_MODEL env var set and
# raise at call time otherwise, so they are not in the default set. The default tools are sync; the
# async agent dispatches them via asyncio.to_thread, so no wrapping is needed.
_TOOL_GROUPS = {
    "web": builtin.web,
    "fs": builtin.fs,
    "compute": builtin.compute,
    "time": builtin.time,
    "misc": builtin.misc,
    "image": builtin.image,
    "audio": builtin.audio,
    "speech": builtin.speech,
    "transcription": builtin.transcription,
}


def _check_group_name(name: str) -> None:
    """Raise ``ValueError`` naming the valid groups if ``name`` is not one of them."""
    if name not in _TOOL_GROUPS and name not in ("all", "none"):
        valid = ", ".join(sorted(_TOOL_GROUPS)) + ", all, none"
        raise ValueError(f"unknown tool group {name!r}; choose from: {valid}")


def enabled_tool_groups(config: AssistantConfig) -> set[str]:
    """The built-in groups workers may draw from, validated.

    Also the one place ``[tools].groups`` is checked. Nothing else expands that list any more (the
    supervisor mounts no built-in groups, and a role's own ``groups`` are intersected with this set and
    otherwise ignored), so without an explicit check a typo like ``groups = ["complete"]`` would
    silently produce tool-less workers instead of failing at startup.
    """
    for name in config.tools:
        _check_group_name(name)
    if "all" in config.tools:
        return set(_TOOL_GROUPS)
    return {g for g in config.tools if g != "none"}


def _resolve_builtin_tools(names: list[str]) -> list:
    """Map tool-group names to built-in tool callables (deduped by name).

    ``"all"`` expands to ``builtin.ALL_TOOLS``; ``"none"`` contributes nothing. An unknown name
    raises ``ValueError`` listing the valid groups.
    """
    resolved: list = []
    seen: set[str] = set()
    for name in names:
        if name == "none":
            continue
        _check_group_name(name)
        group = builtin.ALL_TOOLS if name == "all" else _TOOL_GROUPS[name]
        for fn in group:
            if fn.__name__ not in seen:
                seen.add(fn.__name__)
                resolved.append(fn)
    return resolved


def unreferenced_mcp_servers(config: AssistantConfig) -> list[str]:
    """Configured MCP servers that no sub-agent role names, and so reach no agent at all.

    The supervisor mounts no MCP callables (see ``build_agent``), so a server's tools travel only to the
    workers whose roles list it in ``mcp_servers``, by name or by raw URL. A server nobody names still
    connects and still spends its token on the handshake; it is simply unreachable, and nothing said so.
    Returns each one's name (or URL, when unnamed) for the startup warning.
    """
    referenced = {ref for role in config.subagent_roles.values() for ref in role.get("mcp_servers", [])}
    return [s.name or s.url for s in config.mcp_servers if not ({s.url, s.name} & referenced)]


def _build_subagent_agent_types(
    config: AssistantConfig,
    connections: Optional[list] = None,
    plugin_tools_by_pack: Optional[dict[str, list]] = None,
) -> dict[str, dict]:
    """Build AIMU ``agent_types`` (worker specs) from ``config.subagent_roles``.

    Empty in, empty out: with no configured roles there is no worker menu, and ``add_subagent_tool``
    builds an untyped delegate instead.

    A role's worker toolset is the union, deduped by ``__name__``, of:
      - its ``groups`` intersected with the assistant's enabled ``config.tools`` (a role narrows within
        what is enabled, never exceeds it);
      - the tools of any MCP servers it names in ``mcp_servers``, matched against the live
        ``connections`` by the server's configured ``name`` first, then its raw URL;
      - the tools of any tool-packs it names in ``tool_packs`` (from ``plugin_tools_by_pack``);
      - the ``time`` group, added to every role when it is globally enabled, because a worker that
        cannot tell the time is broken whatever its domain. This is the one addition a role does not
        ask for, and it still respects the global set rather than exceeding it.
    Unknown group/server/pack references drop silently, mirroring the forgiving contract for unknown
    groups. ``connections``/``plugin_tools_by_pack`` default to empty (so a role's built-in groups
    resolve even before the MCP/plugin sources are wired). The role ``description`` becomes the first
    line of the built ``system_message`` (AIMU shows it in the tool's role menu).
    """
    connections = connections or []
    plugin_tools_by_pack = plugin_tools_by_pack or {}
    # A role's own group names are matched against this set; unknown ones drop silently, while an
    # unknown name in [tools].groups itself raises here.
    enabled = enabled_tool_groups(config)
    url_by_name = {s.name: s.url for s in config.mcp_servers if getattr(s, "name", None)}
    callables_by_url = {getattr(c, "url", None): getattr(c, "callables", []) for c in connections}
    # Every role also gets the time group, on top of whatever its own `groups` name. A worker scoped to
    # compute or to a single tool-pack still has to resolve "by tomorrow morning", and a role that
    # forgot to ask for a clock produced a worker that silently could not tell the time. Gated on the
    # global set, so this still never grants a tool the user disabled in [tools].groups.
    ambient_tools = _TOOL_GROUPS["time"] if "time" in enabled else []
    agent_types: dict[str, dict] = {}
    for name, role in config.subagent_roles.items():
        sources: list[list] = [_resolve_builtin_tools([g for g in role.get("groups", []) if g in enabled])]
        for ref in role.get("mcp_servers", []):
            sources.append(callables_by_url.get(url_by_name.get(ref, ref), []))
        for pack in role.get("tool_packs", []):
            sources.append(plugin_tools_by_pack.get(pack, []))
        sources.append(ambient_tools)
        tools: list = []
        seen: set[str] = set()
        for fns in sources:
            for fn in fns:
                fname = getattr(fn, "__name__", None)
                if fname and fname not in seen:
                    seen.add(fname)
                    tools.append(fn)
        body = role.get("system_message", "")
        description = role.get("description", name)
        system_message = f"{description}\n\n{body}" if body else description
        agent_types[name] = {"system_message": system_message, "tools": tools}
    return agent_types


def _load_plugin_tools_by_pack(config: AssistantConfig) -> dict[str, list]:
    """Tool-pack tools grouped by pack name (each pack built once), so a sub-agent role can request a
    specific pack's tools by name. Empty when plugins are disabled.

    Grouped rather than flattened because a pack's tools only ever reach a worker through the role that
    names it: the supervisor mounts none of them. A pack that fails to build is skipped rather than
    taking the assistant down with it."""
    if not config.load_plugins:
        return {}
    by_pack: dict[str, list] = {}
    for name, pack in discover_tool_packs().items():
        try:
            by_pack[name] = list(pack.build(config))
        except Exception:
            logger.warning("Tool-pack %r failed to build; skipping.", name, exc_info=True)
    return by_pack


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


def build_memory(config: AssistantConfig) -> tuple[Optional[SemanticMemoryStore], Optional[DocumentStore], list]:
    """Build persistent memory and its tools, or ``(None, None, [])`` when memory is disabled.

    A SemanticMemoryStore holds facts about the user; a DocumentStore holds longer reference
    documents. Both live under the app state dir, so they survive restarts and span conversations
    (unlike per-conversation history). Their tools have distinct names, so both sets coexist on the
    one agent.

    The stores are shared across every per-conversation agent, and turns on different conversations
    run concurrently, so two turns can invoke these sync tools in parallel (each via
    ``asyncio.to_thread``). That is safe because AIMU's stores serialize their own methods internally
    (a re-entrant per-store lock), so no wrapping is needed here.
    """
    if not config.memory:
        return None, None, []
    memory_store = SemanticMemoryStore(persist_path=str(config.memory_path))
    document_store = DocumentStore(persist_path=str(config.documents_path))
    tools = make_memory_tools(memory_store) + make_document_tools(document_store)
    return memory_store, document_store, tools


def build_agent(
    config: AssistantConfig,
    client,
    *,
    notify: Notify,
    oauth_storage_dir: Path,
    connections: list,
    memory_tools: list,
    for_each_agent: Callable,
    reapply_config: Callable,
    tool_approval: Optional[Callable] = None,
    plugin_tools_by_pack: Optional[dict[str, list]] = None,
    subagent_observer: Optional[SubagentObserver] = None,
) -> aio.SkillAgent:
    """Build the supervisor SkillAgent and its tool set (skills, MCP management, memory, config, time).

    ``add_skill_script`` and the MCP tools need the agent (to surface new tools this turn), so they are
    built after it; the SkillAgent re-appends its skills-server tools each run. ``connections`` is the
    live list the MCP tools append to and the boot reconnect / teardown share. ``for_each_agent`` fans a
    runtime add/remove out across every live agent. ``plugin_tools_by_pack`` is not mounted here -- it is
    passed through so ``refresh_workers`` can rebuild the delegate's role toolsets after such a change.
    """
    manager = SkillManager(skill_dirs=[str(config.skills_dir)])
    author_skill = make_skill_authoring_tool(manager, config.skills_dir)
    agent = aio.SkillAgent(
        client,
        tools=[author_skill],
        skill_manager=manager,
        name="assistant",
        concurrent_tool_calls=config.subagents_concurrent,
    )
    by_pack = plugin_tools_by_pack or {}

    # When an MCP server is added/removed at runtime, rebuild each live agent's spawn_subagent so its
    # workers pick up (or drop) the change -- roles snapshot their toolset when the tool is built, so
    # without this a runtime server would only reach workers after the conversation's agent rebuilt.
    # This closure is fanned out (for_each_agent) to every live agent, so `by_pack` here (one agent's
    # snapshot) is applied to siblings too; that is fine because a tool-pack's build() is a pure
    # function of config (no per-call state), so all snapshots hold equivalent tool instances.
    def refresh_workers(a: aio.SkillAgent) -> None:
        rebuild_subagent_tool(a, config, tool_approval, connections, by_pack, subagent_observer)

    # The supervisor's whole toolset: cross-cutting tools that mutate shared, per-conversation state
    # workers must not touch (skills, MCP connections, memory, config), plus the time group, plus (added
    # by wire_agent) the spawn_subagent delegate and the scheduler tools. Built-in groups, tool-packs,
    # and connected-MCP callables are deliberately absent: they live on the workers, scoped per role.
    # The time tools are the exception because the supervisor answers scheduling and "when" questions
    # itself, and delegating a clock read would cost a whole spawn.
    agent.tools = [
        author_skill,
        make_skill_script_tool(agent, manager, config.skills_dir),
        *make_mcp_tools(
            for_each_agent,
            connections,
            notify=notify,
            oauth_storage_dir=oauth_storage_dir,
            config_path=config.config_path,
            refresh_workers=refresh_workers,
        ),
        *memory_tools,
        *make_config_tools(config.config_path, reapply_config),
        *builtin.time,
    ]
    return agent


def wire_agent(
    config: AssistantConfig,
    client,
    *,
    notify: Notify,
    oauth_storage_dir: Path,
    connections: list,
    memory_tools: list,
    tool_approval: Callable,
    scheduler_tools: list,
    for_each_agent: Callable,
    reapply_config: Callable,
    subagent_observer: Optional[SubagentObserver] = None,
) -> aio.SkillAgent:
    """Build a fully-wired SkillAgent: base tools + approval gate + subagent tool + scheduler tools.

    This is everything the assistant needs on every per-conversation agent, in one place so each
    conversation's agent is wired identically.
    """
    # Build tool-packs once here and share: each worker role draws only the packs it names
    # (add_subagent_tool). The supervisor mounts none of them. Empty when plugins are disabled.
    plugin_tools_by_pack = _load_plugin_tools_by_pack(config)
    agent = build_agent(
        config,
        client,
        notify=notify,
        oauth_storage_dir=oauth_storage_dir,
        connections=connections,
        memory_tools=memory_tools,
        for_each_agent=for_each_agent,
        reapply_config=reapply_config,
        tool_approval=tool_approval,
        plugin_tools_by_pack=plugin_tools_by_pack,
        subagent_observer=subagent_observer,
    )
    agent.tool_approval = tool_approval
    add_subagent_tool(
        agent,
        config,
        tool_approval,
        connections=connections,
        plugin_tools_by_pack=plugin_tools_by_pack,
        observer=subagent_observer,
    )
    agent.tools.extend(scheduler_tools)
    return agent


def make_agent_builder(
    config: AssistantConfig,
    *,
    client_factory: Callable[[str], object],
    notify: Notify,
    oauth_storage_dir: Path,
    connections: list,
    memory_tools: list,
    tool_approval: Callable,
    scheduler_tools: list,
    store,
    images_path: Path,
    for_each_agent: Callable,
    reapply_config: Callable,
    subagent_observer: Optional[SubagentObserver] = None,
) -> Callable[[str], aio.SkillAgent]:
    """Return a builder that constructs and restores a per-conversation agent on demand.

    Each call to ``client_factory`` must return a fresh model client: agents share no client, since
    a shared client's ``.messages`` would defeat per-conversation isolation.
    """
    from kokua.core.messages import expand_message_images

    def build(conversation_id: str) -> aio.SkillAgent:
        client = client_factory(conversation_id)
        agent = wire_agent(
            config,
            client,
            notify=notify,
            oauth_storage_dir=oauth_storage_dir,
            connections=connections,
            memory_tools=memory_tools,
            tool_approval=tool_approval,
            scheduler_tools=scheduler_tools,
            for_each_agent=for_each_agent,
            reapply_config=reapply_config,
            subagent_observer=subagent_observer,
        )
        session = store.get(conversation_id)
        if session is not None and session.messages:
            agent.restore(expand_message_images(session.messages, images_path))
        return agent

    return build


def add_subagent_tool(
    agent: aio.SkillAgent,
    config: AssistantConfig,
    tool_approval: Callable,
    *,
    connections: Optional[list] = None,
    plugin_tools_by_pack: Optional[dict[str, list]] = None,
    observer: Optional[SubagentObserver] = None,
) -> None:
    """Append the typed ``spawn_subagent(agent_type, task)`` delegate, the supervisor's only route to a
    domain tool.

    Each spawn clones the active model and gets its role's scoped tool subset: built-in groups
    (intersected with ``config.tools``) plus any MCP servers and tool-packs the role names, resolved
    against ``connections`` and ``plugin_tools_by_pack``. The parent-only stateful tools (memory,
    skills, MCP management, config, scheduling) are deliberately withheld. Concurrent spawns overlap
    under the parent's ``concurrent_tool_calls``; the approval gate is forwarded so a sub-agent's
    gated-tool calls (e.g. execute_python) prompt via the parent rather than running unattended.
    ``observer`` is how a front end shows the spawn's work while it runs.

    ``config.subagent_roles`` is non-empty by the time this runs (``Assistant.create`` rejects an empty
    set), which also satisfies AIMU's rule that ``agent_types`` must not be an empty dict.
    """
    agent.tools.append(
        make_async_subagent_tool(
            agent.model_client.model,
            agent_types=_build_subagent_agent_types(config, connections, plugin_tools_by_pack),
            tool_approval=tool_approval,
            observer=observer,
        )
    )


def rebuild_subagent_tool(
    agent: aio.SkillAgent,
    config: AssistantConfig,
    tool_approval: Optional[Callable],
    connections: list,
    plugin_tools_by_pack: Optional[dict[str, list]],
    observer: Optional[SubagentObserver] = None,
) -> None:
    """Replace an agent's ``spawn_subagent`` tool with a fresh one built from the CURRENT connections.

    Worker roles snapshot their toolset when ``spawn_subagent`` is built, so after a runtime MCP
    add/remove this re-resolves each role (picking up or dropping the changed server's tools)."""
    agent.tools[:] = [t for t in agent.tools if getattr(t, "__name__", None) != SPAWN_SUBAGENT_TOOL_NAME]
    add_subagent_tool(
        agent,
        config,
        tool_approval,
        connections=connections,
        plugin_tools_by_pack=plugin_tools_by_pack,
        observer=observer,
    )
