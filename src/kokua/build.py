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
from aimu.aio.tools.builtin import make_async_subagent_tool
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.skills import SkillManager, make_skill_authoring_tool, make_skill_script_tool
from aimu.tools import builtin
from aimu.tools.builtin import make_document_tools, make_memory_tools

from .config import DEFAULT_SUBAGENT_ROLES, MEMORY_GUIDANCE, SUBAGENT_GUIDANCE, AssistantConfig
from .mcp import make_mcp_tools
from .mcp_auth import Notify
from .plugins import discover_tool_packs

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
    "misc": builtin.misc,
    "image": builtin.image,
    "audio": builtin.audio,
    "speech": builtin.speech,
    "transcription": builtin.transcription,
}


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
        if name == "all":
            group = builtin.ALL_TOOLS
        elif name in _TOOL_GROUPS:
            group = _TOOL_GROUPS[name]
        else:
            valid = ", ".join(sorted(_TOOL_GROUPS)) + ", all, none"
            raise ValueError(f"unknown tool group {name!r}; choose from: {valid}")
        for fn in group:
            if fn.__name__ not in seen:
                seen.add(fn.__name__)
                resolved.append(fn)
    return resolved


def _effective_subagent_roles(config: AssistantConfig) -> dict[str, dict]:
    """Built-in roles with the user's config roles merged over them by name."""
    return {**DEFAULT_SUBAGENT_ROLES, **config.subagent_roles}


def _build_subagent_agent_types(config: AssistantConfig) -> dict[str, dict]:
    """Build AIMU ``agent_types`` from the effective roles.

    Each role's tools are its groups intersected with the assistant's enabled tool groups
    (``config.tools``), so a role can narrow within what is enabled but never exceed it. The role's
    ``description`` is made the first line of the built ``system_message`` (AIMU shows that line in the
    tool's role menu); an omitted ``system_message`` body defaults to just the description.
    """
    # "all" expands to every group; "none" and unknown/disabled groups are dropped silently.
    if "all" in config.tools:
        enabled = set(_TOOL_GROUPS)
    else:
        enabled = {g for g in config.tools if g != "none"}
    agent_types: dict[str, dict] = {}
    for name, role in _effective_subagent_roles(config).items():
        groups = [g for g in role.get("groups", []) if g in enabled]
        body = role.get("system_message", "")
        description = role.get("description", name)
        system_message = f"{description}\n\n{body}" if body else description
        agent_types[name] = {"system_message": system_message, "tools": _resolve_builtin_tools(groups)}
    return agent_types


def _load_plugin_tools(config: AssistantConfig) -> list:
    """Build the tools contributed by installed tool-pack plugins (deduped by name)."""
    tools: list = []
    seen: set[str] = set()
    for name, pack in discover_tool_packs().items():
        try:
            pack_tools = pack.build(config)
        except Exception:
            logger.warning("Tool-pack %r failed to build; skipping.", name, exc_info=True)
            continue
        for fn in pack_tools:
            fname = getattr(fn, "__name__", None)
            if fname and fname not in seen:
                seen.add(fname)
                tools.append(fn)
        logger.info("Loaded tool-pack %r (%d tools).", name, len(pack_tools))
    return tools


def resolve_system_message(config: AssistantConfig) -> str:
    """The system prompt for the model client: base message plus the memory/subagent guidance the
    enabled features need. Shared by the initial build and a runtime model switch."""
    system = config.system_message + (MEMORY_GUIDANCE if config.memory else "")
    return system + (SUBAGENT_GUIDANCE if config.subagents else "")


def build_model_client(config: AssistantConfig, stored: dict):
    """Build the model client for ``config.model``, applying any persisted model override first.

    A persisted model choice wins over ``config.model``, and ``config.model`` is kept in sync so
    ``current_settings()`` and the panel reflect the model actually running. Raises
    ``ModelClientError`` (carrying AIMU's message) instead of the raw ValueError/TypeError so a front
    end can present it rather than a traceback.
    """
    if stored.get("model"):
        config.model = stored["model"]
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
) -> aio.SkillAgent:
    """Build the SkillAgent and its full tool set (skills, MCP management, memory, plugins, built-ins).

    ``add_skill_script`` and the MCP tools need the agent (to surface new tools this turn), so they are
    built after it; the SkillAgent re-appends its skills-server tools each run. Plugin tools are loaded
    here (deduped by name) when enabled. ``connections`` is the live list the MCP tools append to and
    the boot reconnect / teardown share. ``for_each_agent`` fans a runtime add/remove out across every
    live agent; already-connected servers in ``connections`` are attached to this fresh agent directly.
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
    plugin_tools = _load_plugin_tools(config) if config.load_plugins else []
    agent.tools = [
        author_skill,
        make_skill_script_tool(agent, manager, config.skills_dir),
        *make_mcp_tools(
            for_each_agent,
            connections,
            notify=notify,
            oauth_storage_dir=oauth_storage_dir,
            registry_path=config.mcp_servers_path,
        ),
        *memory_tools,
        *plugin_tools,
        *_resolve_builtin_tools(config.tools),
    ]
    # Attach already-connected MCP servers to this fresh agent (runtime-added servers fan out separately).
    existing = {getattr(fn, "__name__", None) for fn in agent.tools}
    for conn in connections:
        for fn in conn.callables:
            if fn.__name__ not in existing:
                agent.tools.append(fn)
                existing.add(fn.__name__)
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
) -> aio.SkillAgent:
    """Build a fully-wired SkillAgent: base tools + approval gate + subagent tool + scheduler tools.

    This is everything the assistant needs on every per-conversation agent, in one place so each
    conversation's agent is wired identically.
    """
    agent = build_agent(
        config,
        client,
        notify=notify,
        oauth_storage_dir=oauth_storage_dir,
        connections=connections,
        memory_tools=memory_tools,
        for_each_agent=for_each_agent,
    )
    agent.tool_approval = tool_approval
    add_subagent_tool(agent, config, tool_approval)
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
) -> Callable[[str], aio.SkillAgent]:
    """Return a builder that constructs and restores a per-conversation agent on demand.

    Each call to ``client_factory`` must return a fresh model client: agents share no client, since
    a shared client's ``.messages`` would defeat per-conversation isolation.
    """
    from .messages import expand_message_images

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
        )
        session = store.get(conversation_id)
        if session is not None and session.messages:
            agent.restore(expand_message_images(session.messages, images_path))
        return agent

    return build


def add_subagent_tool(agent: aio.SkillAgent, config: AssistantConfig, tool_approval: Callable) -> None:
    """Append the typed ``spawn_subagent(agent_type, task)`` tool when sub-agents are enabled (no-op otherwise).

    Each spawn clones the active model and gets its role's tool subset (role groups intersected with
    ``config.tools``); the parent-only stateful tools (memory, skills, MCP management) are deliberately
    withheld. Concurrent spawns overlap under the parent's ``concurrent_tool_calls``; the approval gate
    is forwarded so a sub-agent's gated-tool calls (e.g. execute_python) prompt via the parent rather
    than running unattended.
    """
    if not config.subagents:
        return
    agent.tools.append(
        make_async_subagent_tool(
            agent.model_client.model,
            agent_types=_build_subagent_agent_types(config),
            tool_approval=tool_approval,
        )
    )
