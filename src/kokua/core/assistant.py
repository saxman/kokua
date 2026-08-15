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
import time
from dataclasses import replace
from typing import Callable, Optional

from aimu import aio
from aimu.aio import Channel, ModelConnectionError, RunHandle, Scheduler
from aimu.aio.channels.base import ChannelMessage
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.sessions import Session, TinyDBSessionStore

from kokua.config import table as runtime_settings
from kokua.core.agent_registry import AgentRegistry
from kokua.channels.ui import ChannelUI
from kokua.channels.web import proactive_turn, streaming_conversation
from kokua.core.build import (
    ModelClientError,
    build_memory,
    build_model_client,
    make_agent_builder,
    unreferenced_mcp_servers,
)
from kokua.config import AssistantConfig, ConfigError
from kokua.core.conversations import ConversationBook
from kokua.core.diagnostics import diag_report
from kokua.core.interaction import HumanGate
from kokua.core.subagents import SubagentReporter
from kokua.core.tools import make_conversation_tools
from kokua.mcp.servers import ServerConnection, reconnect_mcp_servers
from kokua.scheduling import make_scheduler_tools
from kokua.core.settings_runtime import SettingsApplier
from kokua.core.turn_gate import TurnGate
from kokua.core.turns import TurnRunner
from kokua.core.turn_registry import TurnInfo, TurnTracker

logger = logging.getLogger(__name__)

# Re-exported so front ends can keep catching `assistant.ModelClientError` (build-time, from build) and
# `assistant.ModelConnectionError` (runtime server-unreachable, from AIMU).
__all__ = ["Assistant", "ModelClientError", "ModelConnectionError"]


class Assistant:
    """A single-user personal assistant wired from AIMU primitives."""

    def __init__(
        self,
        channel: Channel,
        scheduler: Scheduler,
        store: TinyDBSessionStore,
        config: AssistantConfig,
    ):
        self._ui = ChannelUI(channel)
        # One reporter for this connection (Assistant.create runs once per WebSocket connection): every
        # conversation's spawn_subagent reports through it, and it resolves the turn to record into
        # from a contextvar rather than from construction.
        self._subagent_reporter = SubagentReporter(self._ui)
        self._scheduler = scheduler
        self._store = store
        self._config = config
        # A per-conversation agent cache. Assigned by create() once the registry's builder can bind
        # self._approve; agents are built lazily, by which point it exists.
        self._registry: Optional[AgentRegistry] = None
        # Live remote-MCP connections (startup + runtime-added) kept alive for their lifetime
        # and closed on shutdown. Assigned by create().
        self._mcp_servers: list[ServerConnection] = []
        # Persistent memory stores (None when --no-memory). Assigned by create(); persistence is
        # automatic (Chroma PersistentClient / DocumentStore disk writes), so no teardown needed.
        self._memory_store: Optional[SemanticMemoryStore] = None
        self._document_store: Optional[DocumentStore] = None
        # The readers-writer gate: turns on different conversations run concurrently (each is its own
        # "reader", serialized per-conversation by the registry's per-conversation lock); a config
        # mutation is the exclusive "writer" that waits for in-flight turns to drain. Constructed with
        # a lambda (not `self._registry.lock` directly) because `self._registry` is still None here;
        # it is assigned by create() well before any turn or exclusive hold runs.
        self._gate = TurnGate(lambda conversation_id: self._registry.lock(conversation_id))
        # The store, the agent cache, and the active-conversation pointer, kept together so that a
        # switch moves all three atomically. Its registry is bound by create().
        self._book = ConversationBook(store, self._gate, config, on_active_change=self._ui.set_active_conversation)
        # Each reactive turn runs as a background task (a RunHandle) so the serve loop stays free to
        # receive a `/stop` while a turn is in flight. Tracks at most one running turn per conversation
        # (the gate enforces that invariant); backs /stop, /diag, and shutdown cancellation.
        self._tracker = TurnTracker()
        # A per-turn sequence id for the lifecycle log lines.
        self._turn_seq: int = 0
        # Tool approval and plan review: each a single-slot request the serve loop resolves with the
        # user's next message. Both are lock-guarded, so concurrent tool calls (or concurrent planned
        # turns) can never clobber the slot the serve loop is about to resolve.
        self._human = HumanGate(
            self._ui,
            config,
            active_id=lambda: self._book.active_id,
            is_proactive=proactive_turn.get,
            turn_conversation=streaming_conversation.get,
        )
        # Reads, applies, and persists the runtime-mutable settings. Reaches the agent cache through
        # callbacks rather than holding it, since the registry does not exist yet (see create()).
        self._settings = SettingsApplier(
            config,
            self._ui,
            self._gate,
            live_agents=lambda: self._registry.live_agents(),
            cached_ids=lambda: self._registry.cached_ids(),
            agent_for=lambda conversation_id: self._registry.get(conversation_id),
            active_agent=lambda: self._book.agent,
            cancel_active_turn=self._cancel_current_turn,
        )
        # Turn execution, reactive and proactive. Reaches the store and the agent cache through the
        # conversation book, which already owns both.
        self._turns = TurnRunner(
            self._book,
            self._ui,
            self._gate,
            config,
            review_plan=self._human.review_plan,
            push_conversations=self._maybe_push_conversations,
        )

    @classmethod
    async def create(
        cls, config: AssistantConfig, channel: Channel, *, client=None, client_factory=None
    ) -> "Assistant":
        # The assistant is always a lean supervisor, so its only route to a domain tool is a worker.
        # With no roles it could not browse, read a file, or compute; refuse rather than start something
        # that looks running and cannot work.
        if not config.subagent_roles:
            raise ConfigError(
                "no sub-agent roles configured: the assistant delegates all specialized work, so it "
                "needs at least one [subagents.roles.*] in config.toml. Run `kokua config init` to "
                "write a config with the default roles."
            )
        memory_store, document_store, memory_tools = build_memory(config)

        connections: list[ServerConnection] = []
        oauth_storage_dir = config.data_dir / "mcp-oauth"

        # Multiple conversations live in a session store. The active conversation is the most
        # recently updated (a fresh empty one if there are none).
        store = TinyDBSessionStore(str(config.sessions_path))

        scheduler = Scheduler()
        # Construct the assistant first so the registry's builder can bind its approval gate: agents are
        # built lazily (on first get), by which point assistant._approve exists.
        assistant = cls(channel, scheduler, store, config)
        initial_id = assistant._book.adopt_most_recent()
        # config.toml is the single source of settings: the panel and update_config write it, and the
        # CLI already loaded it into `config` at startup. Just mirror the display flags onto the channel.
        for setting in runtime_settings.RUNTIME_SETTINGS:
            if setting.mirror_on_channel:
                assistant._ui.set_display_flag(setting.field, getattr(config, setting.field))
        assistant._mcp_servers = connections  # same list the MCP tools append to / remove from
        assistant._memory_store = memory_store
        assistant._document_store = document_store

        # Per-conversation model clients: an explicit factory wins; else the injected client backs the
        # initial conversation (single-conversation tests) and further conversations build their own;
        # else every conversation builds its own from config.
        if client_factory is not None:
            raw_factory = client_factory
        elif client is not None:

            def raw_factory(conversation_id: str, _client=client, _initial=initial_id):
                return _client if conversation_id == _initial else build_model_client(config)
        else:

            def raw_factory(conversation_id: str):
                return build_model_client(config)

        # Wrap the raw factory so every conversation's client carries the effective generation kwargs
        # the active agent has, not bare provider defaults.
        assistant._settings.layered_factory(raw_factory)
        scheduler_tools, arm_tasks, task_controls = make_scheduler_tools(
            scheduler, config.scheduled_tasks_path, assistant._proactive
        )
        assistant._tasks = task_controls

        # Read-only visibility across conversations, bound to the live book. Built here rather than in
        # build.py because the book is app state, not a config value, so no tool-pack could reach it.
        # Safe this early: these tools read the store through the book and never touch the agent
        # registry, which is bound below.
        conversation_tools = make_conversation_tools(assistant._book, assistant.turn_running)

        # Fan a global tool mutation (MCP add/remove) out across every live conversation's agent. Reads
        # the registry lazily: it is set just below and only ever called at runtime (add/remove) or by the
        # boot reconnect, by which point the registry exists and is populated.
        def for_each_agent(apply: Callable[[object], None]) -> None:
            for agent in assistant._registry.live_agents():
                apply(agent)

        assistant._registry = AgentRegistry(
            make_agent_builder(
                config,
                client_factory=lambda cid: assistant._settings.client_factory(cid),
                notify=channel.send,
                oauth_storage_dir=oauth_storage_dir,
                connections=connections,
                memory_tools=memory_tools,
                tool_approval=assistant._approve,
                scheduler_tools=scheduler_tools,
                conversation_tools=conversation_tools,
                store=store,
                images_path=config.images_path,
                for_each_agent=for_each_agent,
                reapply_config=assistant._settings.apply_one,
                subagent_observer=assistant._subagent_reporter,
            ),
            cap=config.agent_cache_cap,
        )
        assistant._book.bind_registry(assistant._registry)

        # Reconnect MCP servers BEFORE building the first agent, so `connections` is populated when that
        # agent is built: the supervisor's spawn_subagent snapshots `connections` at build time to give
        # MCP-backed workers their tools. The fan-out is a no-op here (no agents are live yet); it just
        # fills `connections`.
        await reconnect_mcp_servers(
            for_each_agent, connections, config, notify=channel.send, oauth_storage_dir=oauth_storage_dir
        )
        for name in unreferenced_mcp_servers(config):
            logger.warning(
                "MCP server %r is configured but no [subagents.roles.*] names it in `mcp_servers`; "
                "the supervisor mounts no MCP tools itself, so this server reaches no agent.",
                name,
            )

        # Build the active conversation's agent (its client is layered by the factory, which also
        # snapshots the provider base for later re-layering).
        assistant._registry.get(assistant._active_id)

        arm_tasks()
        return assistant

    @property
    def _agent(self) -> aio.SkillAgent:
        """The active conversation's agent (built on demand by the registry)."""
        return self._book.agent

    @property
    def _session(self) -> Session:
        """The active conversation's persisted session (fetched fresh each access)."""
        return self._book.session

    @property
    def _active_id(self) -> str:
        return self._book.active_id

    @property
    def active_id(self) -> str:
        """The conversation currently being viewed. Public accessor so a front end doesn't need to
        reach into the conversation book directly."""
        return self._book.active_id

    def turn_running(self, conversation_id: str) -> bool:
        """Whether `conversation_id` has an in-flight turn right now. Public accessor so a front end
        doesn't need to reach into `_tracker` directly (e.g. to decide whether to show a "working"
        indicator on switching into a conversation)."""
        return self._tracker.running(conversation_id)

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
        return self._book.list()

    async def _cancel_current_turn(self) -> None:
        """Cancel the viewed conversation's in-flight turn (if any) and let it settle, so its partial
        state persists to the conversation it belongs to before we switch its agent's client (a model
        switch, unlike a conversation switch, replaces the client under it, so the in-flight turn
        cannot be left running)."""
        info = self._tracker.get(self._active_id)
        if info is not None and not info.handle.done:
            info.handle.cancel()
            try:
                await info.handle.task
            except Exception:
                pass

    async def new_conversation(self) -> str:
        """Start and switch to a new, empty conversation; returns its id."""
        self._human.abandon_all()
        return self._book.create()

    async def select_conversation(self, conversation_id: str) -> None:
        """Switch the active conversation to an existing one; its agent (re)builds from the store."""
        self._human.abandon_all()
        self._book.select(conversation_id)

    async def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation, switching away from it if it was the one being viewed."""
        if conversation_id == self._active_id:
            self._human.abandon_all()
        await self._book.delete(conversation_id, cancel_turn=self._cancel_turn)

    def _cancel_turn(self, conversation_id: str) -> None:
        """Cancel a conversation's in-flight turn without awaiting it."""
        info = self._tracker.get(conversation_id)
        if info is not None and not info.handle.done:
            info.handle.cancel()

    def current_settings(self) -> dict:
        """The effective runtime settings for the web panel to display."""
        return self._settings.current()

    async def apply_settings(self, incoming: dict) -> None:
        """Apply a settings-panel change at runtime and persist it to config.toml."""
        await self._settings.apply_and_persist(incoming)

    def list_tasks(self) -> list[dict]:
        """The scheduled tasks as fields, for a front end that renders its own task rows."""
        return self._tasks.list_tasks()

    def task_action(self, action: str, id_or_name: str) -> str:
        """Run one task lifecycle action, returning the sentence its equivalent tool would.

        The action name reaches this from a front end, so it is looked up in a table rather than
        dispatched on the string. Raises ``ValueError`` for anything not in it.
        """
        actions: dict[str, Callable[[], str]] = {
            "enable": lambda: self._tasks.set_enabled(id_or_name, True),
            "disable": lambda: self._tasks.set_enabled(id_or_name, False),
            "run": lambda: self._tasks.run_now(id_or_name),
            "delete": lambda: self._tasks.cancel(id_or_name),
        }
        run = actions.get(action)
        if run is None:
            raise ValueError(f"unknown task action {action!r}")
        return run()

    async def _maybe_push_conversations(self) -> None:
        """If the channel supports it, send a refreshed conversation list (e.g. after a new title)."""
        await self._ui.push_conversations(self.list_conversations())

    async def run(self) -> None:
        """Serve the channel and run the scheduler concurrently until the channel closes."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._serve_channel())
                tg.create_task(self._scheduler.run())
        finally:
            # Cancel every conversation's turn still running at shutdown and let the cancellations
            # settle (each turn persists its partial state on stop), so no task is left pending.
            turns = self._tracker.all()
            for _conversation_id, info in turns:
                if not info.handle.done:
                    info.handle.cancel()
            if turns:
                await asyncio.gather(*(info.handle.task for _conversation_id, info in turns), return_exceptions=True)
            for conn in self._mcp_servers:
                try:
                    await conn.client.aclose()
                except Exception:
                    logger.debug("Error closing MCP client", exc_info=True)
            self._store.close()

    async def _serve_channel(self) -> None:
        try:
            async for msg in self._ui.receive():
                raw = msg.text or ""
                text = raw.strip().lower()
                if text == "/stop":
                    self._stop_active_turn()
                    continue
                # /diag reports live state (and the wedged turn's async stack) without touching the
                # turn gate, so it still answers when a hung turn is holding it. Handled here, like /stop.
                if text == "/diag":
                    await self._ui.send(self._diag_report())
                    continue
                # While an approval or plan review is outstanding, the next message is its answer,
                # not a new turn. (A `/stop` above still takes priority, cancelling the waiting turn.)
                if self._human.resolve_reply(raw, text):
                    continue
                # `/plan <task>` invokes deep planning for this one turn (the web UI's Plan toggle sends
                # exactly this). Any other message runs a normal, unplanned turn.
                plan_turn = False
                if text == "/plan" or text.startswith("/plan "):
                    task = raw.strip()[len("/plan") :].strip()
                    if not task:
                        await self._ui.send("Usage: /plan <task>")
                        continue
                    msg = replace(msg, text=task)
                    plan_turn = True
                # Start the turn as a background task so the loop keeps reading and a `/stop` can
                # arrive mid-turn. The gate still serializes same-conversation turns (a proactive turn
                # on this conversation can't interleave); different conversations' turns don't block
                # each other. The target conversation is captured now, at submit time, so the turn
                # persists to it even if the user switches _active_id away before the turn finishes.
                conversation_id = self._active_id
                tid = self._turn_seq
                self._turn_seq += 1
                preview = (msg.text or "").strip()[:120]
                logger.info("turn %d submitted for %s: %r", tid, conversation_id, preview)
                handle = RunHandle.start(self._handle(msg, conversation_id=conversation_id, plan=plan_turn, tid=tid))
                self._tracker.add(conversation_id, TurnInfo(handle=handle, started=time.monotonic(), preview=preview))
                handle.task.add_done_callback(lambda _t, cid=conversation_id, h=handle: self._tracker.remove_if(cid, h))
        finally:
            self._scheduler.stop()  # channel closed -> stop the scheduler so run() returns

    def _stop_active_turn(self) -> None:
        """Cancel the viewed conversation's tracked turn (if any); the /stop branch's helper."""
        self._cancel_turn(self._active_id)

    async def _approve(self, name: str, arguments: dict) -> bool:
        return await self._human.approve(name, arguments)

    async def _handle(
        self, msg: ChannelMessage, *, conversation_id: str, plan: bool = False, tid: Optional[int] = None
    ) -> None:
        await self._turns.reactive(msg, conversation_id=conversation_id, plan=plan, tid=tid)

    def _diag_report(self) -> str:
        return diag_report(
            self._tracker,
            self._gate,
            pending_approval=self._human.approval.pending,
            pending_plan=self._human.plan.pending,
        )

    async def _proactive(
        self,
        prompt: str,
        *,
        target: str = "active",
        task_name: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Optional[str]:
        return await self._turns.proactive(
            prompt, target=target, task_name=task_name, session_id=session_id, task_id=task_id
        )
