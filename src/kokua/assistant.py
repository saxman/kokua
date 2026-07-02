"""The assistant core: wires AIMU primitives into a single-user, always-on assistant.

    Channel.receive()  ->  SkillAgent.run()  ->  Channel.send()
              Scheduler  ->  proactive SkillAgent.run()  ->  Channel.send()
              ConversationManager persists history across restarts
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from aimu import PROVENANCE_CONTINUATION, PROVENANCE_KEY, PROVENANCE_PROACTIVE, aio
from aimu.aio import Channel, RunHandle, Scheduler
from aimu.aio.channels.base import ChannelMessage
from aimu.history import ConversationManager
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.sessions import Session, TinyDBSessionStore
from aimu.skills import SkillManager, make_skill_authoring_tool, make_skill_script_tool
from aimu.tools import builtin, tool
from aimu.tools.builtin import make_document_tools, make_memory_tools

from . import mcp_registry, runtime_settings
from .config import MEMORY_GUIDANCE, AssistantConfig
from .mcp_auth import Notify, build_chat_oauth
from .plugins import discover_tool_packs

logger = logging.getLogger(__name__)

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

    Returns the names of the tools newly added. Tools land on ``agent.tools`` (the configured list
    the SkillAgent copies to its model client every run, so they survive the per-run reset) **and**
    on the live ``model_client.tools`` dispatch list. The live append is what makes a server added
    mid-turn callable in the *same* turn: ``_prepare_run`` only rebuilds ``model_client.tools`` at
    the start of a run, so without it the new tools are absent from this turn's dispatch table and a
    call returns "tool not found". Mirrors ``SkillAgent.reload_skills``. (At boot ``_prepare_run``
    rebuilds from ``agent.tools`` anyway, so the live append is simply redundant there.)
    """
    new_tools = await client.as_tools()
    existing = {getattr(fn, "__name__", None) for fn in agent.tools}
    added = [fn for fn in new_tools if fn.__name__ not in existing]
    agent.tools.extend(added)
    live_existing = {getattr(fn, "__name__", None) for fn in agent.model_client.tools}
    agent.model_client.tools = list(agent.model_client.tools) + [fn for fn in added if fn.__name__ not in live_existing]
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
        # Drop from both the configured list (future runs) and the live dispatch list (this turn).
        agent.tools[:] = [fn for fn in agent.tools if getattr(fn, "__name__", None) not in removed]
        agent.model_client.tools = [
            fn for fn in agent.model_client.tools if getattr(fn, "__name__", None) not in removed
        ]
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


def _import_last_history(store: TinyDBSessionStore, config: AssistantConfig) -> None:
    """One-time migration: import the last single-conversation history.json into the store."""
    history_path = Path(config.history_path)
    if not history_path.exists():
        return
    manager = ConversationManager(str(history_path), use_last_conversation=True)
    messages = [dict(m) for m in manager.messages]
    manager.close()
    if not messages:
        return
    now = datetime.now().isoformat()
    store.save(
        Session(
            key=uuid.uuid4().hex,
            messages=messages,
            metadata={"title": _derive_title(messages), "created_at": now, "updated_at": now},
        )
    )


def _backfill_continuation_provenance(store: TinyDBSessionStore) -> int:
    """Tag legacy agent-loop continuation turns persisted before the provenance key existed.

    Turns the agent loop injected before AIMU added ``provenance`` were stored as ordinary
    ``{"role": "user"}`` messages, so history replay showed them as user bubbles. Match them by their
    default continuation-prompt text (kokua never overrides ``continuation_prompt``) and tag them.
    Idempotent: already-tagged and non-matching messages are skipped and only changed sessions are
    re-saved, so it is safe to run on every startup. Returns the number of messages tagged.
    """
    from aimu.aio.agent import DEFAULT_CONTINUATION_PROMPT

    tagged = 0
    for key in store.list_keys():
        session = store.get(key)
        changed = False
        for message in session.messages:
            if (
                message.get("role") == "user"
                and PROVENANCE_KEY not in message
                and message.get("content") == DEFAULT_CONTINUATION_PROMPT
            ):
                message[PROVENANCE_KEY] = PROVENANCE_CONTINUATION
                tagged += 1
                changed = True
        if changed:
            store.save(session)
    return tagged


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
        # True while a proactive (unprompted) turn runs, so _approve auto-denies gated tools (no
        # user is waiting to confirm).
        self._in_proactive = False
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
            client = aio.client(config.model, system=system)

        # Snapshot the provider's built-in generate kwargs, then layer config.toml + persisted runtime
        # values on top, and apply persisted display prefs. Runs for injected clients (tests) too.
        base_generate_kwargs = dict(client.default_generate_kwargs)
        _layer_generate_kwargs(client, base_generate_kwargs, config, stored.get("generate_kwargs", {}))
        _apply_show_flags(channel, config, stored)

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
        agent = aio.SkillAgent(client, tools=[author_skill], skill_manager=manager, name="assistant")
        # add_skill_script and the MCP tools need the agent (to surface new tools this turn), so
        # they are built after it. Built-in tools, memory tools, and plugin tools are appended too;
        # the SkillAgent re-appends its skills-server tools each run.
        connections: list[_ServerConnection] = []
        oauth_storage_dir = config.data_dir / "mcp-oauth"
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
            *_resolve_builtin_tools(config.tools),
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

        # Multiple conversations live in a session store. On first run (no store yet), import the
        # last single-conversation history.json so the existing chat is not lost. The active
        # conversation is the most recently updated (a fresh empty one if there are none).
        first_run = not config.sessions_path.exists()
        store = TinyDBSessionStore(str(config.sessions_path))
        if first_run:
            _import_last_history(store, config)
        _backfill_continuation_provenance(store)
        session = _active_session(store)
        if session.messages:
            agent.restore(session.messages)

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
            self._agent.restore(session.messages)
        return session.key

    async def select_conversation(self, conversation_id: str) -> None:
        """Switch the active conversation to an existing one and restore it into the agent."""
        await self._cancel_current_turn()
        async with self._lock:
            self._session = self._store.get(conversation_id)
            self._agent.restore(self._session.messages)

    def current_settings(self) -> dict:
        """The effective runtime settings for the web panel to display: model, prefs, generate kwargs."""
        return {
            "model": str(self._config.model) if self._config.model else "",
            "show_thinking": getattr(self._channel, "show_thinking", self._config.show_thinking),
            "show_tools": getattr(self._channel, "show_tools", self._config.show_tools),
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
        new_client = aio.client(model, system=system)  # build first; only swap on success
        self._agent.model_client = new_client
        self._agent.restore(self._session.messages)
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
                text = (msg.text or "").strip().lower()
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
                # Start the turn as a background task so the loop keeps reading and a `/stop` can
                # arrive mid-turn. Turns stay serialized by self._lock (a reminder can't interleave).
                handle = RunHandle.start(self._handle(msg))
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
        """
        if name not in self._config.confirm_tools:
            return True
        if self._in_proactive:
            return False
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

    async def _handle(self, msg: ChannelMessage) -> None:
        async with self._lock:
            try:
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
        messages = [dict(m) for m in self._agent.model_client.messages]
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
