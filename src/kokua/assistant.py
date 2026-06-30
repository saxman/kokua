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
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from aimu import aio
from aimu.aio import Channel, RunHandle, Scheduler
from aimu.aio.channels.base import ChannelMessage
from aimu.history import ConversationManager
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.sessions import Session, TinyDBSessionStore
from aimu.skills import SkillManager, make_skill_authoring_tool, make_skill_script_tool
from aimu.tools import builtin, tool
from aimu.tools.builtin import make_document_tools, make_memory_tools

from .config import MEMORY_GUIDANCE, AssistantConfig
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


def make_add_mcp_server_tool(agent: aio.SkillAgent, mcp_clients: list) -> Callable:
    """Build an ``add_mcp_server`` tool bound to ``agent`` and a live-client registry.

    Lets the assistant connect to a remote MCP service by URL mid-session and use its tools
    immediately. The new tools are appended to ``agent.tools`` (the configured list the
    SkillAgent copies to its model client every run, so they persist across turns), deduped by
    name. The connected client is kept in ``mcp_clients`` for the connection's lifetime.
    """

    @tool
    async def add_mcp_server(url: str, bearer_token: Optional[str] = None) -> str:
        """Connect to a remote MCP server by URL and add its tools to this assistant.

        The server's tools become callable immediately, even in this same turn. Returns the names
        of the newly available tools.

        Authentication is automatic: just pass the URL. If the server is unprotected it connects
        directly; if it requires OAuth, a browser window opens for you to authorize and the token
        is captured and used for the rest of the session, no token needed up front. Pass
        bearer_token only to use a static token instead of the OAuth flow.
        """
        try:
            if bearer_token:
                mcp = await aio.MCPClient.connect(url=url, auth=bearer_token)
            else:
                try:
                    mcp = await aio.MCPClient.connect(url=url)
                except Exception as exc:
                    if not _looks_like_auth_required(exc):
                        raise
                    # The server challenged for auth; run the OAuth flow. FastMCP discovers the
                    # auth server, opens a browser to authorize, captures the token via a local
                    # callback, and uses it for this session.
                    logger.info("MCP server %s requires authorization; starting OAuth flow.", url)
                    mcp = await aio.MCPClient.connect(url=url, auth="oauth")
            new_tools = await mcp.as_tools()
        except Exception as exc:
            return f"Failed to connect to MCP server {url!r}: {exc}"
        mcp_clients.append(mcp)
        existing = {getattr(fn, "__name__", None) for fn in agent.tools}
        added = [fn for fn in new_tools if fn.__name__ not in existing]
        agent.tools.extend(added)
        names = ", ".join(fn.__name__ for fn in added) if added else "(no new tools)"
        return f"Connected to {url}. Tools now available: {names}."

    return add_mcp_server


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
        self._mcp_clients: list = []
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

    @classmethod
    async def create(cls, config: AssistantConfig, channel: Channel, *, client=None) -> "Assistant":
        if client is None:
            system = config.system_message + (MEMORY_GUIDANCE if config.memory else "")
            client = aio.client(config.model, system=system)

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
        # add_skill_script and add_mcp_server need the agent (to surface new tools this turn), so
        # they are built after it. Built-in tools, memory tools, and plugin tools are appended too;
        # the SkillAgent re-appends its skills-server tools each run.
        mcp_clients: list = []
        agent.tools = [
            author_skill,
            make_skill_script_tool(agent, manager, config.skills_dir),
            make_add_mcp_server_tool(agent, mcp_clients),
            *memory_tools,
            *plugin_tools,
            *_resolve_builtin_tools(config.tools),
        ]

        # Connect any startup MCP servers; their tools persist on agent.tools. A connect failure
        # logs and continues so one unreachable server can't stop the assistant from starting.
        for url in config.mcp_servers:
            try:
                mcp = await aio.MCPClient.connect(url=url, auth=config.mcp_bearer)
                agent.tools.extend(await mcp.as_tools())
                mcp_clients.append(mcp)
            except Exception:
                logger.warning("Could not connect MCP server %s; continuing without it.", url, exc_info=True)

        # Multiple conversations live in a session store. On first run (no store yet), import the
        # last single-conversation history.json so the existing chat is not lost. The active
        # conversation is the most recently updated (a fresh empty one if there are none).
        first_run = not config.sessions_path.exists()
        store = TinyDBSessionStore(str(config.sessions_path))
        if first_run:
            _import_last_history(store, config)
        session = _active_session(store)
        if session.messages:
            agent.restore(session.messages)

        scheduler = Scheduler()
        assistant = cls(agent, channel, scheduler, store, session, config)
        assistant._mcp_clients = mcp_clients  # same list the add_mcp_server tool appends to
        assistant._memory_store = memory_store
        assistant._document_store = document_store
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
            for mcp in self._mcp_clients:
                try:
                    await mcp.aclose()
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
                self._persist()
                return
            except Exception:
                logger.exception("Error handling message")
                await self._channel.send("Sorry, something went wrong handling that.", reply_to=msg)
            self._persist()

    async def _proactive(self) -> None:
        """Scheduled callback: produce a message unprompted and push it to the channel."""
        async with self._lock:
            self._in_proactive = True
            try:
                reply = await self._agent.run(self._config.reminder_text)
                await self._channel.send(reply)
                self._persist()
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
