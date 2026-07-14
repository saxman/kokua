"""The assistant core: wires AIMU primitives into a single-user, always-on assistant.

    Channel.receive()  ->  SkillAgent.run()  ->  Channel.send()
              Scheduler  ->  proactive SkillAgent.run()  ->  Channel.send()
              a TinyDBSessionStore persists conversations across restarts
              author_skill / add_skill_script let the assistant grow its own skills
              memory tools give it persistent facts + documents
              tool-pack plugins contribute extra tools

Kept transport-agnostic (it takes a `Channel`), so the CLI and web front ends share it
unchanged. The CLI/web entry points live in `kokua.cli` / `kokua.frontends`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from aimu import PROVENANCE_KEY, PROVENANCE_PROACTIVE, aio
from aimu.aio import Channel, RunHandle, Scheduler
from aimu.aio.channels.base import ChannelMessage
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.sessions import Session, TinyDBSessionStore
from aimu.aio.tools.builtin import make_async_subagent_tool
from aimu.skills import SkillManager, make_skill_authoring_tool, make_skill_script_tool
from aimu.tools import builtin, tool
from aimu.tools.builtin import make_document_tools, make_memory_tools

from . import images, mcp_registry, review, runtime_settings
from .config import DEFAULT_SUBAGENT_ROLES, MEMORY_GUIDANCE, SUBAGENT_GUIDANCE, AssistantConfig
from .mcp_auth import Notify, build_chat_oauth
from .plugins import discover_tool_packs

logger = logging.getLogger(__name__)

# Deep planning mode prompts. The plan phase runs the agent (tools enabled, so it can web-search and
# consult its skill catalog) to produce an explicit plan without doing the work; the execute phase then
# carries out the approved plan. Planning is scratch work kept out of the saved conversation (see
# _make_plan), and the executor's synthetic turn is rewritten back to the user's words (see _planned_turn).
PLAN_PROMPT = """\
Before doing any work, produce an explicit plan for how you will accomplish the request below. Do NOT \
carry out the work or produce the final deliverable yet -- only plan.

Request:
{request}

Your plan should:
- State the goal and what a complete, correct answer looks like.
- Give the concrete steps you will take, in order, as a numbered markdown list.
- For each step, name the specific tool, skill, or MCP service you will use. Where a needed capability \
is missing, say so and how you will get it: build a new skill (author_skill), connect an MCP service \
(add_mcp_server), and web-search to find a suitable MCP service or documentation when that helps.
- Note what you will verify before finishing.

You may use read-only tools (e.g. web search) to inform the plan, but make no changes yet. Respond with \
just the plan."""

EXECUTE_PROMPT = """\
Carry out the following approved plan to fully answer the original request. Follow the plan, adapting if \
you discover something that requires it, and use the tools/skills it names.

Original request:
{request}

Approved plan:
{plan}"""

# Feedback blocks fed back into a replan / revise round after an adversarial reviewer rejects.
REPLAN_FEEDBACK = "\n\nAn independent reviewer rejected your previous plan for these reasons:\n{issues}\n\nProduce a new plan that addresses them."

RESULT_REVISE_PROMPT = """\
Your previous answer was checked by an independent reviewer and rejected. Revise it to fully address the \
issues, returning the complete corrected answer (not just the changes).

Original request:
{request}

Approved plan:
{plan}

Your previous answer:
{answer}

Reviewer's issues:
{issues}"""


def _bullets(issues: list[str]) -> str:
    """Render reviewer issues as a markdown bullet list (or a dash if empty)."""
    return "\n".join(f"- {i}" for i in issues) or "- (no specific issues given)"


def _tool_evidence(messages: list[dict], max_chars: int = 2000) -> str:
    """Render the tool results in ``messages`` (an executor transcript slice) as a compact evidence block
    for the result reviewer, so it judges against what the agent actually retrieved rather than its own
    (possibly stale) memory. Each tool result is truncated to ``max_chars``. Returns "" if no tools ran."""
    names: dict = {}  # tool_call_id -> tool name, to label results that lack a "name" of their own
    lines: list[str] = []
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            names[call.get("id")] = call.get("function", {}).get("name")
        if msg.get("role") == "tool":
            name = msg.get("name") or names.get(msg.get("tool_call_id")) or "tool"
            content = str(msg.get("content", ""))
            if len(content) > max_chars:
                content = content[:max_chars] + " ...[truncated]"
            lines.append(f"- {name}: {content}")
    return "\n".join(lines)


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
    enabled = set(config.tools)
    agent_types: dict[str, dict] = {}
    for name, role in _effective_subagent_roles(config).items():
        groups = [g for g in role.get("groups", []) if g in enabled]
        body = role.get("system_message", "")
        description = role.get("description", name)
        system_message = f"{description}\n\n{body}" if body else description
        agent_types[name] = {"system_message": system_message, "tools": _resolve_builtin_tools(groups)}
    return agent_types


def _looks_like_auth_required(exc: BaseException) -> bool:
    """Heuristic: did this connection failure come from an auth challenge (so OAuth should run)?

    Matches the failure text against common auth signals (401/403, "unauthorized", a
    WWW-Authenticate / OAuth hint). Deliberately narrow so a plain unreachable host (DNS,
    connection refused) does not trigger an OAuth attempt.
    """
    text = f"{exc} {getattr(exc, '__cause__', '') or ''}".lower()
    return any(s in text for s in ("401", "403", "unauthor", "forbidden", "www-authenticate", "oauth"))


@dataclass
class _ServerConnection:
    """A live remote-MCP connection and the tools it contributed (for teardown and removal)."""

    url: str
    client: Any  # aio.MCPClient
    tools: list[str]  # __name__ of each tool this server added to agent.tools
    auth_mode: str  # "none" | "oauth" | "bearer"


async def _connect_mcp(
    url: str,
    *,
    bearer_token: Optional[str] = None,
    auth_mode: Optional[str] = None,
    notify: Notify,
    oauth_storage_dir: Path,
) -> tuple[Any, str]:
    """Connect to a remote MCP server, returning ``(client, auth_mode_used)``.

    With ``auth_mode`` known (a boot reconnect) the connection uses that mode directly. With it
    ``None`` (a runtime add) the connection tries unauthenticated first and falls back to the OAuth
    flow on an auth challenge. A ``bearer_token`` always takes precedence. OAuth posts an
    authorization link via ``notify`` and persists tokens under ``oauth_storage_dir`` (so a cached
    token reconnects silently).
    """
    if bearer_token:
        return await aio.MCPClient.connect(url=url, auth=bearer_token), "bearer"
    if auth_mode == "oauth":
        provider = build_chat_oauth(url, notify=notify, token_storage_dir=oauth_storage_dir)
        return await aio.MCPClient.connect(url=url, auth=provider), "oauth"
    if auth_mode == "none":
        return await aio.MCPClient.connect(url=url), "none"
    # Unknown (runtime add): try unauthenticated, fall back to OAuth on an auth challenge.
    try:
        return await aio.MCPClient.connect(url=url), "none"
    except Exception as exc:
        if not _looks_like_auth_required(exc):
            raise
        logger.info("MCP server %s requires authorization; starting OAuth flow.", url)
        provider = build_chat_oauth(url, notify=notify, token_storage_dir=oauth_storage_dir)
        return await aio.MCPClient.connect(url=url, auth=provider), "oauth"


async def _attach_server(agent: aio.SkillAgent, connections: list, url: str, client: Any, auth_mode: str) -> list[str]:
    """Add a connected server's tools to the agent (deduped) and record the connection.

    Returns the names of the tools newly added. Tools land on ``agent.tools``; the tool-loop engine
    re-reads the agent's effective tools each round, so a server added mid-turn is dispatchable in the
    same turn without touching the model client.
    """
    new_tools = await client.as_tools()
    existing = {getattr(fn, "__name__", None) for fn in agent.tools}
    added = [fn for fn in new_tools if fn.__name__ not in existing]
    agent.tools.extend(added)
    names = [fn.__name__ for fn in added]
    connections.append(_ServerConnection(url=url, client=client, tools=names, auth_mode=auth_mode))
    return names


def make_mcp_tools(
    agent: aio.SkillAgent,
    connections: list,
    *,
    notify: Notify,
    oauth_storage_dir: Path,
    registry_path: Path,
) -> list[Callable]:
    """Build the ``add_mcp_server`` / ``remove_mcp_server`` tools bound to one connection registry.

    Lets the assistant connect to (and disconnect from) a remote MCP service by URL mid-session.
    A reconnectable server (unauthenticated or OAuth) is recorded in ``registry_path`` so it
    reconnects on the next restart; bearer-token servers are session-only (their secret is not
    written to disk). ``connections`` is the live list shared with the boot path and teardown.
    """

    @tool
    async def add_mcp_server(url: str, bearer_token: Optional[str] = None) -> str:
        """Connect to a remote MCP server by URL and add its tools to this assistant.

        The server's tools become callable immediately, even in this same turn, and the connection
        is remembered so it is restored automatically the next time the assistant starts. Returns
        the names of the newly available tools.

        Authentication is handled for you: just pass the URL. If the server is unprotected it
        connects directly. If it requires OAuth, you post an authorization link into the chat and
        open a browser window for the user to approve; once they do, the connection completes and
        the token is saved for future sessions. Do not claim you cannot authenticate or that this
        is impossible from here, that flow is built in. Pass bearer_token only when the user gives
        you a static token to use instead of the OAuth flow.
        """
        if any(conn.url == url for conn in connections):
            return f"Already connected to {url}; its tools are available. Use remove_mcp_server to disconnect first."
        try:
            client, auth_mode = await _connect_mcp(
                url, bearer_token=bearer_token, notify=notify, oauth_storage_dir=oauth_storage_dir
            )
            added = await _attach_server(agent, connections, url, client, auth_mode)
        except Exception as exc:
            return f"Failed to connect to MCP server {url!r}: {exc}"
        # Persist reconnectable servers (no secret on disk); a bearer server stays session-only.
        if auth_mode in mcp_registry.RECONNECTABLE:
            mcp_registry.add(registry_path, url, auth_mode)
            note = ""
        else:
            note = " (session only; add it to config.toml [mcp] to keep a bearer-token server across restarts)"
        names = ", ".join(added) if added else "(no new tools)"
        return f"Connected to {url}. Tools now available: {names}.{note}"

    @tool
    async def remove_mcp_server(url: str) -> str:
        """Disconnect a remote MCP server added earlier and remove its tools.

        Drops the server's tools, closes the connection, and forgets it so it is not reconnected on
        the next restart. Pass the same URL that was used to add it.
        """
        entry = next((c for c in connections if c.url == url), None)
        if entry is None:
            return f"No MCP server is connected at {url!r}."
        removed = set(entry.tools)
        # Drop from agent.tools; the engine re-reads the effective tools each round, so the tools
        # stop being advertised and dispatchable from the next round on (this turn included).
        agent.tools[:] = [fn for fn in agent.tools if getattr(fn, "__name__", None) not in removed]
        connections.remove(entry)
        try:
            await entry.client.aclose()
        except Exception:
            logger.debug("Error closing MCP client for %s", url, exc_info=True)
        mcp_registry.remove(registry_path, url)
        names = ", ".join(sorted(removed)) if removed else "(none)"
        return f"Disconnected {url}. Removed tools: {names}."

    return [add_mcp_server, remove_mcp_server]


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


def _message_text(content) -> str:
    """Plain text of a message's content (a string, or the text blocks of a multimodal list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _derive_title(messages: list[dict]) -> Optional[str]:
    """A conversation title from the first user message (stripped, truncated), or None."""
    for message in messages:
        if message.get("role") == "user":
            text = _message_text(message.get("content")).strip()
            if text:
                return text[:40]
    return None


def _map_image_block_urls(messages: list[dict], transform) -> list[dict]:
    """Return a copy of *messages* with each ``image_url`` block's url passed through *transform*.

    ``transform`` returns a replacement url, or ``None`` to leave the block unchanged. Only messages that
    actually contain an image_url block are copied; the rest are shared by reference (cheap, safe: the
    caller never mutates in place)."""
    out: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list) or not any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in content
        ):
            out.append(message)
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url", "")
                replacement = transform(url)
                if replacement is not None:
                    block = {**block, "image_url": {**block["image_url"], "url": replacement}}
            new_content.append(block)
        out.append({**message, "content": new_content})
    return out


def _compact_message_images(messages: list[dict], images_path) -> list[dict]:
    """Rewrite inline base64 image data URLs to on-disk ``/images/<hash>`` references (for persistence).

    Keeps ``sessions.json`` small: the bytes are written under ``images_path`` (content-addressed) and the
    stored message keeps only the short reference. A url that is already a reference or an http URL is left
    as-is."""

    def to_reference(url: str):
        if url.startswith("data:"):
            return images.save_data_url(images_path, url)
        return None

    return _map_image_block_urls(messages, to_reference)


def _expand_message_images(messages: list[dict], images_path) -> list[dict]:
    """Rewrite ``/images/<name>`` references back to base64 data URLs (before restoring into the agent).

    The model request must carry pixels (a localhost /images URL is not fetchable by the provider), so a
    reference is re-read from disk here. A reference whose file is missing is left unchanged rather than
    crashing the restore."""

    def to_data_url(url: str):
        if images.is_reference(url):
            return images.reference_to_data_url(images_path, url)
        return None

    return _map_image_block_urls(messages, to_data_url)


def _active_session(store: TinyDBSessionStore) -> Session:
    """The most-recently-updated session, creating a fresh empty one if the store is empty."""
    keys = store.list_keys()
    if keys:
        sessions = [store.get(key) for key in keys]
        sessions.sort(key=lambda s: s.metadata.get("updated_at", ""), reverse=True)
        return sessions[0]
    now = datetime.now().isoformat()
    session = Session(key=uuid.uuid4().hex, metadata={"created_at": now, "updated_at": now})
    store.save(session)
    return session


def _layer_generate_kwargs(client, base: dict, config: AssistantConfig, runtime: dict) -> None:
    """Rebuild the client's default generate kwargs in place, layering the runtime override on top.

    Order (later wins): provider built-in defaults (`base`) < config.toml `[generation]` < the runtime
    values the settings panel set. Only keys present in a layer are applied, so a key the user never
    set (e.g. presence_penalty on Anthropic) is never injected.
    """
    kwargs = client.default_generate_kwargs
    kwargs.clear()
    kwargs.update(base)
    kwargs.update(config.generation)
    kwargs.update(runtime)


def _apply_show_flags(channel: Channel, config: AssistantConfig, settings: dict) -> None:
    """Apply show_thinking / show_tools from a settings dict to the config and channel (if it has them)."""
    for flag in ("show_thinking", "show_tools"):
        if flag in settings:
            setattr(config, flag, settings[flag])
            if hasattr(channel, flag):
                setattr(channel, flag, settings[flag])


class Assistant:
    """A single-user personal assistant wired from AIMU primitives."""

    def __init__(
        self,
        agent: aio.SkillAgent,
        channel: Channel,
        scheduler: Scheduler,
        store: TinyDBSessionStore,
        session: Session,
        config: AssistantConfig,
    ):
        self._agent = agent
        self._channel = channel
        self._scheduler = scheduler
        self._store = store
        self._session = session
        self._config = config
        # Live remote-MCP connections (startup + runtime-added) kept alive for their lifetime
        # and closed on shutdown. Assigned by create().
        self._mcp_servers: list[_ServerConnection] = []
        # Persistent memory stores (None when --no-memory). Assigned by create(); persistence is
        # automatic (Chroma PersistentClient / DocumentStore disk writes), so no teardown needed.
        self._memory_store: Optional[SemanticMemoryStore] = None
        self._document_store: Optional[DocumentStore] = None
        # The reactive turn and a proactive turn share one agent/client; serialize them so
        # a reminder firing mid-conversation can't interleave on shared message state.
        self._lock = asyncio.Lock()
        # Each reactive turn runs as a background task (a RunHandle) so the serve loop stays free to
        # receive a `/stop` while a turn is in flight. `_current` is the latest turn (the one `/stop`
        # cancels); `_turns` keeps task refs alive until they finish.
        self._current: Optional[RunHandle] = None
        self._turns: set = set()
        # Tool-approval coordination. At most one approval is pending at a time (turns are
        # serialized by self._lock); the serve loop resolves the future with the user's answer.
        self._pending_approval: Optional[asyncio.Future] = None
        # Serializes the gated-tool approval path so concurrent tool calls (concurrent_tool_calls) can
        # never have two approvals pending at once (which would clobber self._pending_approval). Only
        # gated tools acquire it; ungated tools and proactive auto-deny never touch it.
        self._approval_lock = asyncio.Lock()
        # Plan-review coordination (deep planning mode): while a plan awaits the user's
        # approve/edit/reject, the serve loop resolves the future. Mirrors the approval gate.
        self._pending_plan: Optional[asyncio.Future] = None
        self._pending_plan_text = ""
        # True while a proactive (unprompted) turn runs, so _approve auto-denies gated tools (no
        # user is waiting to confirm).
        self._in_proactive = False
        # The raw trace of the in-flight verbose turn: a list of {label, detail, text} phase segments,
        # accumulated by _send_phase / _run_and_capture / _stream_review and persisted for reload.
        # None outside a verbose turn (so shared helpers used by non-verbose turns don't capture).
        self._trace: Optional[list[dict]] = None
        # The active model client's provider built-in generate kwargs, snapshotted before any override
        # is layered on, so a settings change (or a cleared field) can rebuild from a clean base.
        # Assigned by create() and refreshed on a runtime model switch.
        self._base_generate_kwargs: dict = {}

    @classmethod
    async def create(cls, config: AssistantConfig, channel: Channel, *, client=None) -> "Assistant":
        # Runtime-mutable settings the web panel persisted: generation kwargs, display prefs, and the
        # active model. Layered over config.toml (which is never rewritten); see runtime_settings.
        stored = runtime_settings.load(config.runtime_settings_path)
        if client is None:
            # A persisted model choice wins over config.model, and config.model is kept in sync so
            # current_settings() and the panel reflect the model actually running.
            if stored.get("model"):
                config.model = stored["model"]
            system = config.system_message + (MEMORY_GUIDANCE if config.memory else "")
            system += SUBAGENT_GUIDANCE if config.subagents else ""
            client = aio.client(config.model, system=system)

        # Snapshot the provider's built-in generate kwargs, then layer config.toml + persisted runtime
        # values on top, and apply persisted display prefs. Runs for injected clients (tests) too.
        base_generate_kwargs = dict(client.default_generate_kwargs)
        _layer_generate_kwargs(client, base_generate_kwargs, config, stored.get("generate_kwargs", {}))
        _apply_show_flags(channel, config, stored)
        for flag in (
            "plan_review",
            "plan_review_agent",
            "result_review",
            "show_reasoning",
        ):  # config-only toggles
            if flag in stored:
                setattr(config, flag, stored[flag])

        # Persistent memory: a SemanticMemoryStore for facts about the user and a DocumentStore for
        # longer reference documents. Both live under the app state dir, so they survive restarts and
        # span conversations (unlike per-conversation history). Tools have distinct names, so both
        # sets coexist on the one agent.
        memory_store: Optional[SemanticMemoryStore] = None
        document_store: Optional[DocumentStore] = None
        memory_tools: list = []
        if config.memory:
            memory_store = SemanticMemoryStore(persist_path=str(config.memory_path))
            document_store = DocumentStore(persist_path=str(config.documents_path))
            memory_tools = make_memory_tools(memory_store) + make_document_tools(document_store)

        plugin_tools = _load_plugin_tools(config) if config.load_plugins else []

        manager = SkillManager(skill_dirs=[str(config.skills_dir)])
        author_skill = make_skill_authoring_tool(manager, config.skills_dir)
        agent = aio.SkillAgent(
            client,
            tools=[author_skill],
            skill_manager=manager,
            name="assistant",
            concurrent_tool_calls=config.subagents_concurrent,
        )
        # add_skill_script and the MCP tools need the agent (to surface new tools this turn), so
        # they are built after it. Built-in tools, memory tools, and plugin tools are appended too;
        # the SkillAgent re-appends its skills-server tools each run.
        connections: list[_ServerConnection] = []
        oauth_storage_dir = config.data_dir / "mcp-oauth"
        builtin_tools = _resolve_builtin_tools(config.tools)

        # Sub-agents (on by default): a typed spawn_subagent(agent_type, task) tool. Each spawn clones the
        # active model and gets its role's tool subset (role groups intersected with config.tools); the
        # parent-only stateful tools (memory, skills, MCP management) are deliberately withheld. Concurrent
        # spawns overlap under the parent's concurrent_tool_calls (set on the SkillAgent below); the
        # approval gate stays correct because _approve serializes only the gated-tool path (see _approve).
        subagent_tools = (
            [make_async_subagent_tool(client.model, agent_types=_build_subagent_agent_types(config))]
            if config.subagents
            else []
        )

        agent.tools = [
            author_skill,
            make_skill_script_tool(agent, manager, config.skills_dir),
            *make_mcp_tools(
                agent,
                connections,
                notify=channel.send,
                oauth_storage_dir=oauth_storage_dir,
                registry_path=config.mcp_servers_path,
            ),
            *memory_tools,
            *plugin_tools,
            *subagent_tools,
            *builtin_tools,
        ]

        # Reconnect MCP servers at boot so their tools are available without re-adding them: first
        # the ones declared in config (--mcp / [mcp] servers), then the ones added at runtime and
        # recorded in the registry (deduped by URL). A connect failure logs and continues so one
        # unreachable server can't stop the assistant from starting.
        for url in config.mcp_servers:
            try:
                client_, mode = await _connect_mcp(
                    url, bearer_token=config.mcp_bearer, notify=channel.send, oauth_storage_dir=oauth_storage_dir
                )
                await _attach_server(agent, connections, url, client_, mode)
            except Exception:
                logger.warning("Could not connect MCP server %s; continuing without it.", url, exc_info=True)

        connected_urls = {conn.url for conn in connections}
        for record in mcp_registry.load(config.mcp_servers_path):
            url = record["url"]
            if url in connected_urls:
                continue
            try:
                client_, mode = await _connect_mcp(
                    url, auth_mode=record.get("auth_mode"), notify=channel.send, oauth_storage_dir=oauth_storage_dir
                )
                await _attach_server(agent, connections, url, client_, mode)
            except Exception:
                logger.warning("Could not reconnect MCP server %s; continuing without it.", url, exc_info=True)

        # Multiple conversations live in a session store. The active conversation is the most
        # recently updated (a fresh empty one if there are none).
        store = TinyDBSessionStore(str(config.sessions_path))
        session = _active_session(store)
        if session.messages:
            agent.restore(_expand_message_images(session.messages, config.images_path))

        scheduler = Scheduler()
        assistant = cls(agent, channel, scheduler, store, session, config)
        assistant._mcp_servers = connections  # same list the MCP tools append to / remove from
        assistant._memory_store = memory_store
        assistant._document_store = document_store
        assistant._base_generate_kwargs = base_generate_kwargs
        # Gate configured "risky" tools behind interactive approval (see _approve). Published to the
        # model client on every run by the agent's _prepare_run; an empty confirm_tools is a no-op.
        agent.tool_approval = assistant._approve
        if config.reminder_seconds is not None:
            scheduler.at(config.reminder_seconds, assistant._proactive, name="reminder")
        return assistant

    @property
    def history(self) -> list[dict]:
        """The active conversation's messages (OpenAI-format), for a front end to display."""
        return self._session.messages

    @property
    def history_metadata(self) -> dict:
        """The active conversation's metadata (e.g. the ``subagent`` map), for replay display."""
        return self._session.metadata

    def list_conversations(self) -> list[dict]:
        """All conversations as {id, title, updated_at, active}, most-recently-updated first."""
        items = []
        for key in self._store.list_keys():
            session = self._store.get(key)
            items.append(
                {
                    "id": key,
                    "title": session.metadata.get("title") or "New conversation",
                    "updated_at": session.metadata.get("updated_at", ""),
                    "active": key == self._session.key,
                }
            )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return items

    async def _cancel_current_turn(self) -> None:
        """Cancel any in-flight turn and let it settle, so its partial state persists to the
        conversation it belongs to before we switch away."""
        if self._current is not None and not self._current.done:
            self._current.cancel()
            try:
                await self._current.task
            except Exception:
                pass

    async def new_conversation(self) -> str:
        """Start and switch to a new, empty conversation; returns its id."""
        await self._cancel_current_turn()
        async with self._lock:
            now = datetime.now().isoformat()
            session = Session(key=uuid.uuid4().hex, metadata={"created_at": now, "updated_at": now})
            self._store.save(session)
            self._session = session
            self._agent.restore(_expand_message_images(session.messages, self._config.images_path))
        return session.key

    async def select_conversation(self, conversation_id: str) -> None:
        """Switch the active conversation to an existing one and restore it into the agent."""
        await self._cancel_current_turn()
        async with self._lock:
            self._session = self._store.get(conversation_id)
            self._agent.restore(_expand_message_images(self._session.messages, self._config.images_path))

    async def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation. If it is the active one, switch to the most-recently-updated
        remaining conversation (or a fresh empty one if none remain) and restore it into the agent."""
        deleting_active = conversation_id == self._session.key
        if deleting_active:
            await self._cancel_current_turn()
        async with self._lock:
            self._store.delete(conversation_id)
            if deleting_active:
                self._session = _active_session(self._store)
                self._agent.restore(_expand_message_images(self._session.messages, self._config.images_path))

    def current_settings(self) -> dict:
        """The effective runtime settings for the web panel to display: model, prefs, generate kwargs."""
        return {
            "model": str(self._config.model) if self._config.model else "",
            "show_thinking": getattr(self._channel, "show_thinking", self._config.show_thinking),
            "show_tools": getattr(self._channel, "show_tools", self._config.show_tools),
            "plan_review": self._config.plan_review,
            "plan_review_agent": self._config.plan_review_agent,
            "result_review": self._config.result_review,
            "show_reasoning": self._config.show_reasoning,
            "generate_kwargs": dict(self._agent.model_client.default_generate_kwargs),
        }

    async def apply_settings(self, incoming: dict) -> None:
        """Apply a settings-panel change at runtime and persist it so it survives restarts.

        Generation-kwargs and display-pref changes are applied in place under the turn lock. Switching
        the model rebuilds the model client (mirroring select_conversation: cancel the in-flight turn,
        then restore conversation state onto the new client). A model that fails to build leaves the
        running client untouched.
        """
        settings = runtime_settings.sanitize(incoming)
        new_model = settings.get("model")
        switching = bool(new_model) and new_model != (str(self._config.model) if self._config.model else "")

        if switching:
            await self._cancel_current_turn()
        async with self._lock:
            if switching:
                await self._switch_model(new_model)
            _apply_show_flags(self._channel, self._config, settings)
            for flag in (
                "plan_review",
                "plan_review_agent",
                "result_review",
                "show_reasoning",
            ):  # config-only
                if flag in settings:
                    setattr(self._config, flag, settings[flag])
            _layer_generate_kwargs(
                self._agent.model_client, self._base_generate_kwargs, self._config, settings["generate_kwargs"]
            )
            runtime_settings.save(self._config.runtime_settings_path, settings)

    async def _switch_model(self, model: str) -> None:
        """Rebuild the model client for a new model, carrying over conversation state and tools.

        Tools bind the agent (not the client), so they survive the swap and are republished to the new
        client on the next run. Raises (leaving the old client in place) if the model can't be built.
        """
        system = self._config.system_message + (MEMORY_GUIDANCE if self._config.memory else "")
        system += SUBAGENT_GUIDANCE if self._config.subagents else ""
        new_client = aio.client(model, system=system)  # build first; only swap on success
        self._agent.model_client = new_client
        self._agent.restore(_expand_message_images(self._session.messages, self._config.images_path))
        self._config.model = model
        self._base_generate_kwargs = dict(new_client.default_generate_kwargs)

    async def _maybe_push_conversations(self) -> None:
        """If the channel supports it, send a refreshed conversation list (e.g. after a new title)."""
        send = getattr(self._channel, "send_conversations", None)
        if send is not None:
            await send(self.list_conversations())

    async def run(self) -> None:
        """Serve the channel and run the scheduler concurrently until the channel closes."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._serve_channel())
                tg.create_task(self._scheduler.run())
        finally:
            # Cancel any turn still running at shutdown and let the cancellations settle (each turn
            # persists its partial state on stop), so no task is left pending.
            for task in list(self._turns):
                task.cancel()
            if self._turns:
                await asyncio.gather(*self._turns, return_exceptions=True)
            for conn in self._mcp_servers:
                try:
                    await conn.client.aclose()
                except Exception:
                    logger.debug("Error closing MCP client", exc_info=True)
            self._store.close()

    async def _serve_channel(self) -> None:
        try:
            async for msg in self._channel.receive():
                raw = msg.text or ""
                text = raw.strip().lower()
                if text == "/stop":
                    if self._current is not None and not self._current.done:
                        self._current.cancel()
                    continue
                # While an approval is pending, the next message is the answer, not a new turn.
                # (A `/stop` above still takes priority, cancelling the turn that is awaiting it.)
                pending = self._pending_approval
                if pending is not None and not pending.done():
                    pending.set_result(text in ("y", "yes"))
                    continue
                # While a plan awaits review, the next message is the approve/edit/reject decision.
                plan_pending = self._pending_plan
                if plan_pending is not None and not plan_pending.done():
                    if text in ("approve", "yes", "y"):
                        plan_pending.set_result(self._pending_plan_text)
                    elif text in ("reject", "no", "n"):
                        plan_pending.set_result(None)
                    elif text.startswith("edit:"):
                        plan_pending.set_result(raw.split(":", 1)[1].strip() or self._pending_plan_text)
                    else:
                        plan_pending.set_result(raw.strip())  # any other text is an edited plan
                    continue
                # `/plan <task>` invokes deep planning for this one turn (the web UI's Plan toggle sends
                # exactly this). Any other message runs a normal, unplanned turn.
                plan_turn = False
                if text == "/plan" or text.startswith("/plan "):
                    task = raw.strip()[len("/plan") :].strip()
                    if not task:
                        await self._channel.send("Usage: /plan <task>")
                        continue
                    msg = replace(msg, text=task)
                    plan_turn = True
                # Start the turn as a background task so the loop keeps reading and a `/stop` can
                # arrive mid-turn. Turns stay serialized by self._lock (a reminder can't interleave).
                handle = RunHandle.start(self._handle(msg, plan=plan_turn))
                self._current = handle
                self._turns.add(handle.task)
                handle.task.add_done_callback(self._turns.discard)
        finally:
            self._scheduler.stop()  # channel closed -> stop the scheduler so run() returns

    async def _approve(self, name: str, arguments: dict) -> bool:
        """Tool-approval gate run before each tool call (published to the model client per run).

        Ungated tools pass. A proactive (unprompted) turn auto-denies a gated tool, since no user is
        waiting to confirm and an unprompted full-access call is what approval guards against.
        Otherwise prompt over the channel and await the answer, which the serve loop routes here.

        Gated approvals are serialized by ``self._approval_lock``: with concurrent tool calls a round can
        invoke several tools at once, but only one approval is ever pending, so the single
        ``self._pending_approval`` future the serve loop resolves is never clobbered.
        """
        if name not in self._config.confirm_tools:
            return True
        if self._in_proactive:
            return False
        async with self._approval_lock:
            self._pending_approval = asyncio.get_running_loop().create_future()
            try:
                await self._prompt_approval(name, arguments)
                return await self._pending_approval
            finally:
                # Cleared here so a `/stop` that cancels the turn mid-await (raising CancelledError out
                # of the await) still leaves no stale pending approval.
                self._pending_approval = None

    async def _prompt_approval(self, name: str, arguments: dict) -> None:
        """Ask the user to approve a tool call, however the channel can (web frame vs. plain text)."""
        request = getattr(self._channel, "send_approval_request", None)
        if request is not None:
            await request(name, arguments)
        else:
            await self._channel.send(f"[approve] Allow {name}({arguments})? [y/N]")

    async def _handle(self, msg: ChannelMessage, plan: bool = False) -> None:
        # Planning is opt-in per turn (the web Plan toggle or a `/plan <task>` message sets plan=True).
        do_plan = plan
        async with self._lock:
            try:
                if do_plan:
                    await self._planned_turn(msg)
                else:
                    stream = await self._agent.run(msg.text, stream=True, images=msg.images)
                    await self._channel.send(stream, reply_to=msg)
            except asyncio.CancelledError:
                # `/stop` (or shutdown) cancelled this turn. Note it, keep the partial state (the
                # agent snapshots it in a finally), and return so the daemon keeps serving.
                try:
                    await self._channel.send("(stopped)", reply_to=msg)
                except Exception:
                    pass
                if self._persist():
                    await self._maybe_push_conversations()
                return
            except Exception:
                logger.exception("Error handling message")
                await self._channel.send("Sorry, something went wrong handling that.", reply_to=msg)
            if self._persist():
                await self._maybe_push_conversations()

    async def _planned_turn(self, msg: ChannelMessage) -> None:
        """Deep planning: plan, optionally adversarially review + human review, then execute (optionally
        with adversarial result review). Sub-agent (reviewer) activity is shown live and recorded per turn
        (keyed by the turn's user-message index) so it replays on reload."""
        if self._config.show_reasoning and getattr(self._channel, "send_phase", None) is not None:
            await self._verbose_planned_turn(msg)  # verbose trace needs a phase-capable channel (web)
            return
        events: list[dict] = []  # sub-agent verdicts for this turn (for persistence/replay)
        plan_text = await self._make_plan(msg)
        critique: Optional[list[str]] = None
        if self._config.plan_review_agent:
            plan_text, critique = await self._adversarial_plan_review(msg, plan_text, events)
        await self._send_plan(plan_text, critique)
        approved = plan_text
        if self._config.plan_review:
            approved = await self._review_plan(plan_text, critique)
            if approved is None:  # rejected; no committed turn to anchor the reviewer cards to
                await self._channel.send("(plan rejected)", reply_to=msg)
                return
        if self._config.result_review:
            # Review the answer before showing it -> cannot stream; buffer, vet, then send a plain string.
            answer = await self._execute_reviewed(msg, approved, events)
            self._record_subagent(len(self._agent.model_client.messages) - 2, events)  # [..., user, assistant]
            await self._channel.send(answer, reply_to=msg)
            return
        # Execute the approved plan, streamed. The executor's prompt weaves in the plan; afterwards we
        # rewrite the synthetic user turn back to the user's own words so the saved conversation stays clean.
        base_len = len(self._agent.model_client.messages)
        stream = await self._agent.run(
            EXECUTE_PROMPT.format(request=msg.text, plan=approved), stream=True, images=msg.images
        )
        await self._channel.send(stream, reply_to=msg)
        msgs = self._agent.model_client.messages
        if len(msgs) > base_len and msgs[base_len].get("role") == "user":
            msgs[base_len]["content"] = msg.text
        self._record_subagent(base_len, events)

    async def _verbose_planned_turn(self, msg: ChannelMessage) -> None:
        """Deep planning with the full trace visible: every LLM call streams under a labeled phase and
        every plan/result version is shown, including each reviewer's prose reasoning. The whole raw
        trace is captured (self._trace) and persisted for reload -- verbose turns show the raw output,
        not summary cards. Only the final answer is committed to the conversation; this overrides
        result_review's gate.
        """
        self._trace = []  # active trace: phase segments captured by the streaming helpers below
        try:
            await self._send_phase("Planner", "drafting a plan")
            plan = await self._make_plan(msg, show_answer=True)
            critique: Optional[list[str]] = None
            if self._config.plan_review_agent:
                plan, critique = await self._verbose_plan_review(msg, plan)
            approved = plan
            if self._config.plan_review:  # optional human gate still applies
                approved = await self._review_plan(plan, critique)
                if approved is None:
                    await self._channel.send("(plan rejected)", reply_to=msg)
                    return
            await self._verbose_execute(msg, approved)  # streams + commits the final answer
            self._record_trace(len(self._agent.model_client.messages) - 2, self._trace)  # [..., user, asst]
            await self._send_done()
        finally:
            self._trace = None

    async def _verbose_plan_review(self, msg: ChannelMessage, plan: str) -> tuple[str, Optional[list[str]]]:
        """Stream each plan-review round's prose reasoning; re-plan visibly on rejection. No summary
        card -- the streamed reasoning (and a following 'revising' phase on rejection) is the output."""
        rounds = self._config.review_rounds
        for attempt in range(rounds + 1):
            await self._send_phase("Plan reviewer", f"round {attempt + 1}")
            verdict = await self._stream_review(review.stream_plan_review(self._config.model, msg.text, plan))
            if verdict.approved:
                return plan, None
            if attempt == rounds:
                return plan, verdict.issues
            await self._send_phase("Planner", "revising the plan")
            plan = await self._make_plan(msg, feedback=verdict.issues, show_answer=True)
        return plan, None

    async def _verbose_execute(self, msg: ChannelMessage, plan: str) -> str:
        """Stream the executor and each result-review round visibly; every version is shown. Commits only
        the final answer to a clean transcript."""
        base = list(self._agent.model_client.messages)
        rounds = self._config.review_rounds
        answer = ""
        try:
            await self._send_phase("Executor", "carrying out the plan")
            answer = await self._run_and_capture(
                EXECUTE_PROMPT.format(request=msg.text, plan=plan), msg.images, show_answer=True
            )
            if self._config.result_review:
                for attempt in range(rounds + 1):
                    await self._send_phase("Result reviewer", f"round {attempt + 1}")
                    evidence = _tool_evidence(self._agent.model_client.messages[len(base) :])
                    verdict = await self._stream_review(
                        review.stream_result_review(self._config.model, msg.text, plan, answer, evidence),
                    )
                    if verdict.approved or attempt == rounds:
                        break
                    await self._send_phase("Executor", "revising the answer")
                    self._agent.model_client.messages = list(base)  # revise from a clean base
                    answer = await self._run_and_capture(
                        RESULT_REVISE_PROMPT.format(
                            request=msg.text, plan=plan, answer=answer, issues=_bullets(verdict.issues)
                        ),
                        msg.images,
                        show_answer=True,
                    )
        finally:
            pair = [{"role": "user", "content": msg.text}, {"role": "assistant", "content": answer}]
            self._agent.model_client.messages = base + (pair if answer else [])
        return answer

    async def _stream_review(self, open_coro) -> "review.Verdict":
        """Stream a reviewer's prose reasoning live (captured into the current phase segment for replay),
        then finalize and return its verdict. Emits no summary card -- the prose is the output."""
        client, stream = await open_coro
        stream_activity = getattr(self._channel, "stream_activity", None)
        if stream_activity is not None:
            text = await stream_activity(stream, show_answer=True)
        else:  # no streaming channel: drain so the reviewer call completes
            text = ""
            async for _ in stream:
                pass
        if self._trace:  # attach the reviewer's prose to the current phase segment
            self._trace[-1]["text"] = text
        return await review.finalize_verdict(client)

    async def _send_done(self) -> None:
        """End a verbose turn: finalize the last streamed bubble and clear the processing state."""
        send = getattr(self._channel, "send_done", None)
        if send is not None:
            await send()

    def _record_subagent(self, user_index: int, events: list[dict]) -> None:
        """Record this turn's reviewer verdicts under its user-message index for reload replay.

        Persisted by ``_persist`` (which saves ``session.metadata``). No-op when nothing was reviewed.
        """
        if events and user_index >= 0:
            self._session.metadata.setdefault("subagent", {})[str(user_index)] = events

    def _record_trace(self, user_index: int, trace: list[dict]) -> None:
        """Record a verbose turn's full raw trace (phase label/detail + streamed text) under its
        user-message index, so reload replays the same raw output instead of summary cards.

        Persisted by ``_persist`` (which saves ``session.metadata``). No-op when the trace is empty.
        """
        if trace and user_index >= 0:
            self._session.metadata.setdefault("trace", {})[str(user_index)] = trace

    async def _send_subagent(self, event: dict) -> None:
        """Show a sub-agent activity card if the channel supports it (web); other channels ignore it."""
        send = getattr(self._channel, "send_subagent", None)
        if send is not None:
            await send(event)

    async def _make_plan(
        self, msg: ChannelMessage, feedback: Optional[list[str]] = None, *, show_answer: bool = False
    ) -> str:
        """Run the agent to produce a plan, keeping the planning exchange out of the saved conversation.

        Tools stay enabled so the planner can web-search and consult its skill catalog; the turns it adds
        (planner prompt, tool calls, plan) are rolled back afterwards -- planning is scratch work, and the
        approved plan is re-supplied to the executor in _planned_turn. ``feedback`` (reviewer issues) drives
        a re-plan round; ``show_answer`` streams the plan text live (verbose trace).
        """
        prompt = PLAN_PROMPT.format(request=msg.text)
        if feedback:
            prompt += REPLAN_FEEDBACK.format(issues=_bullets(feedback))
        base = list(self._agent.model_client.messages)
        try:
            plan = await self._run_and_capture(prompt, msg.images, show_answer=show_answer)
        finally:
            self._agent.model_client.messages = base
        return plan

    async def _run_and_capture(self, prompt: str, images, *, show_answer: bool = False) -> str:
        """Run the agent, showing its agentic loop (thinking/tool calls) live, and return the final text.

        By default the final text is withheld (the caller shows it once it's ready). With
        ``show_answer=True`` (verbose trace) the text is streamed live too. Channels without
        ``stream_activity`` (e.g. the CLI) fall back to a plain non-streaming run.
        """
        stream_activity = getattr(self._channel, "stream_activity", None)
        if stream_activity is None:
            result = await self._agent.run(prompt, images=images)
            text = result if isinstance(result, str) else str(result)
        else:
            stream = await self._agent.run(prompt, stream=True, images=images)
            text = await stream_activity(stream, show_answer=show_answer)
        if self._trace:  # verbose trace: attach this call's output to the current phase segment
            self._trace[-1]["text"] = text
        return text

    async def _send_phase(self, label: str, detail: str = "") -> None:
        """Announce a labeled phase (verbose trace) if the channel supports it; others ignore it.

        Also opens a new segment in the in-flight trace (self._trace) so the streamed output that
        follows is captured under this phase for reload replay.
        """
        if self._trace is not None:
            self._trace.append({"label": label, "detail": detail, "text": ""})
        send = getattr(self._channel, "send_phase", None)
        if send is not None:
            await send(label, detail)

    async def _run_review(self, sid: str, role: str, round_: int, coro) -> "review.Verdict":
        """Show a running sub-agent card, await the reviewer, then update the card with its verdict."""
        await self._send_subagent({"id": sid, "role": role, "status": "running", "round": round_})
        verdict = await coro
        status = "approved" if verdict.approved else "rejected"
        await self._send_subagent(
            {"id": sid, "role": role, "status": status, "issues": list(verdict.issues), "round": round_}
        )
        return verdict

    @staticmethod
    def _verdict_event(role: str, round_: int, verdict: "review.Verdict") -> dict:
        """The persisted (id-less) form of a reviewer verdict, for replay."""
        status = "approved" if verdict.approved else "rejected"
        return {"role": role, "status": status, "issues": list(verdict.issues), "round": round_}

    async def _adversarial_plan_review(
        self, msg: ChannelMessage, plan: str, events: list[dict]
    ) -> tuple[str, Optional[list[str]]]:
        """Have an independent, context-free agent critique the plan; re-plan on rejection up to
        review_rounds. Emits reviewer cards, appends verdicts to ``events``, and returns the final plan and
        any residual issues (None if the reviewer approved)."""
        rounds = self._config.review_rounds
        for attempt in range(rounds + 1):
            verdict = await self._run_review(
                f"plan-review-{attempt}",
                "Plan reviewer",
                attempt,
                review.review_plan(self._config.model, msg.text, plan),
            )
            events.append(self._verdict_event("Plan reviewer", attempt, verdict))
            if verdict.approved:
                return plan, None
            if attempt == rounds:  # out of rounds; carry the unresolved issues forward
                return plan, verdict.issues
            plan = await self._make_plan(msg, feedback=verdict.issues)
        return plan, None  # unreachable (rounds >= 0)

    async def _execute_reviewed(self, msg: ChannelMessage, plan: str, events: list[dict]) -> str:
        """Execute non-streaming, have an independent agent review the answer, revise on rejection up to
        review_rounds, then commit a single clean turn (user's words + final answer) and return it. Emits
        reviewer cards and appends verdicts to ``events``."""
        base = list(self._agent.model_client.messages)
        rounds = self._config.review_rounds
        answer = ""
        try:
            answer = await self._run_and_capture(EXECUTE_PROMPT.format(request=msg.text, plan=plan), msg.images)
            for attempt in range(rounds + 1):
                evidence = _tool_evidence(self._agent.model_client.messages[len(base) :])
                verdict = await self._run_review(
                    f"result-review-{attempt}",
                    "Result reviewer",
                    attempt,
                    review.review_result(self._config.model, msg.text, plan, answer, evidence),
                )
                events.append(self._verdict_event("Result reviewer", attempt, verdict))
                if verdict.approved:
                    break
                if attempt == rounds:
                    answer += "\n\n---\n_Automated review flagged unresolved issues:_\n" + _bullets(verdict.issues)
                    break
                self._agent.model_client.messages = list(base)  # revise from a clean base
                answer = await self._run_and_capture(
                    RESULT_REVISE_PROMPT.format(
                        request=msg.text, plan=plan, answer=answer, issues=_bullets(verdict.issues)
                    ),
                    msg.images,
                )
        finally:
            # Commit one clean turn; the executor's scratch (and revision rounds) stay out of history.
            pair = [{"role": "user", "content": msg.text}, {"role": "assistant", "content": answer}]
            self._agent.model_client.messages = base + (pair if answer else [])
        return answer

    async def _send_plan(self, plan_text: str, critique: Optional[list[str]] = None) -> None:
        """Show the plan (with any residual reviewer concerns), as a plan frame if the channel supports it."""
        text = plan_text
        if critique:
            text += "\n\n---\n**Reviewer's remaining concerns:**\n" + _bullets(critique)
        send = getattr(self._channel, "send_plan", None)
        if send is not None:
            await send(text)
        else:
            await self._channel.send(f"Plan:\n\n{text}")

    async def _review_plan(self, plan_text: str, critique: Optional[list[str]] = None) -> Optional[str]:
        """Await the user's decision on a plan: approve (the plan), edit (their text), or reject (None).

        Mirrors _approve: create a future, prompt the channel, and let the serve loop resolve it. Any
        adversarial-reviewer critique is surfaced with the prompt so the human can weigh it.
        """
        self._pending_plan = asyncio.get_running_loop().create_future()
        self._pending_plan_text = plan_text
        try:
            await self._prompt_plan_review(plan_text, critique)
            return await self._pending_plan
        finally:
            self._pending_plan = None
            self._pending_plan_text = ""

    async def _prompt_plan_review(self, plan_text: str, critique: Optional[list[str]] = None) -> None:
        """Ask the user to review a plan, however the channel can (web frame vs. plain text)."""
        request = getattr(self._channel, "send_plan_review_request", None)
        if request is not None:
            await request(plan_text, _bullets(critique) if critique else None)
        else:
            note = ("\nReviewer's concerns:\n" + _bullets(critique)) if critique else ""
            await self._channel.send("[plan] Reply 'approve', 'reject', or 'edit: <revised plan>'." + note)

    async def _proactive(self) -> None:
        """Scheduled callback: produce a message unprompted and push it to the channel."""
        async with self._lock:
            self._in_proactive = True
            try:
                # Tag every message this unprompted run appends so replayed history can distinguish it
                # from a user-driven turn. The agent doesn't reset on run (system prompt lives on the
                # client), so the pre-run length is a stable start index for the exchange.
                start = len(self._agent.model_client.messages)
                reply = await self._agent.run(self._config.reminder_text)
                for message in self._agent.model_client.messages[start:]:
                    message[PROVENANCE_KEY] = PROVENANCE_PROACTIVE
                await self._channel.send(reply)
                if self._persist():
                    await self._maybe_push_conversations()
            finally:
                self._in_proactive = False

    def _persist(self) -> bool:
        """Snapshot the agent's messages onto the active session and save. Returns True if a title
        was just derived (first user message), so a caller can refresh the conversation list."""
        messages = _compact_message_images(
            [dict(m) for m in self._agent.model_client.messages], self._config.images_path
        )
        self._session.messages = messages
        title_set = False
        if not self._session.metadata.get("title"):
            title = _derive_title(messages)
            if title:
                self._session.metadata["title"] = title
                title_set = True
        self._session.metadata["updated_at"] = datetime.now().isoformat()
        self._store.save(self._session)
        return title_set
