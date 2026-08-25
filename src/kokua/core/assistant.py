"""The assistant core: wires AIMU primitives into a single-user, always-on assistant.

    Channel.receive()  ->  SkillAgent.run()  ->  Channel.send()
              Scheduler  ->  proactive SkillAgent.run()  ->  Channel.send()
              a TinyDBSessionStore persists conversations across restarts
              author_skill / add_skill_script let the assistant grow its own skills
              memory tools give it persistent facts + documents
              plugin toolsets contribute extra capabilities an agent can declare

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
from aimu.sessions import Session, TinyDBSessionStore

from kokua.config.settings_sources import build_settings_table
from kokua.core.agent_registry import AgentRegistry
from kokua.channels.ui import ChannelUI
from kokua.channels.web import proactive_turn, streaming_conversation
from kokua.core.build import (
    ModelClientError,
    build_model_client,
    entry_agent_system_message,
    make_agent_builder,
    model_label,
)
from kokua.config import AssistantConfig
from kokua.core.conversations import ConversationBook
from kokua.core.diagnostics import diag_report
from kokua.core.interaction import HumanGate
from kokua.core.subagents import SubagentReporter
from kokua.mcp.auth import OAuthSettings
from kokua.mcp.servers import ServerConnection, reconnect_mcp_servers
from kokua.core.settings_runtime import SettingsApplier
from kokua.core.turn_gate import TurnGate
from kokua.core.turns import TurnRunner
from kokua.core.turn_registry import TurnInfo, TurnTracker
from kokua.registry.context import LiveState

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
        # The live settings table: Kokua's own entries plus whatever the installed toolsets declared.
        # Built here because the applier below needs it, and shared with the config toolset through
        # LiveState so a hot `update_config` resolves against the same table `apply_settings` does.
        self._settings_table = build_settings_table()
        # One reporter for this connection (Assistant.create runs once per WebSocket connection): every
        # conversation's spawn_subagent reports through it, and it resolves the turn to record into
        # from a contextvar rather than from construction.
        self._subagent_reporter = SubagentReporter(
            self._ui, model_for=config.model_for, thinking_for=config.thinking_for
        )
        self._scheduler = scheduler
        self._store = store
        self._config = config
        # A per-conversation agent cache. Assigned by create() once the registry's builder can bind
        # self._approve; agents are built lazily, by which point it exists.
        self._registry: Optional[AgentRegistry] = None
        # Live remote-MCP connections (startup + runtime-added) kept alive for their lifetime
        # and closed on shutdown. Assigned by create().
        self._mcp_servers: list[ServerConnection] = []
        # Every toolset's shared live state (memory/document stores, connections, the registry, ...).
        # Assigned by create(); the ``_memory_store`` / ``_document_store`` properties read through it.
        self._state: Optional[LiveState] = None
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
        # The workflow commands this assistant answers, by command word. Built by create() from the
        # entry agent's declared toolsets; empty until then, which no serve loop can observe.
        self._workflows: dict[str, object] = {}
        # Commands an *installed* workflow-bearing toolset offers that the entry agent did not declare,
        # by command word, mapping to the toolset's name. Built by create() alongside self._workflows.
        # Kept distinct from it because the two need opposite handling in the serve loop: one runs the
        # workflow, the other must not run a plain turn on the literal "/word ..." text.
        self._undeclared_workflow_commands: dict[str, str] = {}
        # Tool approval and a workflow's own decision: each a single-slot request the serve loop
        # resolves with the user's next message. Both are lock-guarded, so concurrent tool calls (or
        # concurrent workflow turns) can never clobber the slot the serve loop is about to resolve.
        self._human = HumanGate(
            self._ui,
            config,
            active_id=lambda: self._book.active_id,
            is_proactive=proactive_turn.get,
            turn_conversation=streaming_conversation.get,
        )
        # Reads, applies, and persists the runtime-mutable settings. Reaches the live state through a
        # callback rather than holding it, since it does not exist yet (see create()).
        self._settings = SettingsApplier(
            config,
            self._gate,
            table=self._settings_table,
            state=lambda: self._state,
        )
        # Turn execution, reactive and proactive. Reaches the store and the agent cache through the
        # conversation book, which already owns both.
        self._turns = TurnRunner(
            self._book,
            self._ui,
            self._gate,
            config,
            tracker=self._tracker,
            decide=self._human.decide,
            push_conversations=self._maybe_push_conversations,
            delete_conversation=self.delete_conversation,
        )

    @classmethod
    async def create(
        cls, config: AssistantConfig, channel: Channel, *, client=None, client_factory=None
    ) -> "Assistant":
        # Imported here, not at module level: kokua.core.agents pulls in kokua.toolsets.core, which
        # pulls in kokua.core.transcripts -- a submodule of this package -- and importing it triggers
        # kokua/core/__init__ to run, which imports this module. A top-level import here would close
        # that cycle.
        from kokua.core.agents import (
            build_command_map,
            configured_but_undeclared,
            undeclared_workflow_commands,
            validate_confirm_tools,
            validated_registry,
        )

        # Built and validated before anything else in this method, because everything else touches
        # something: the next statements open a session store (which mints and persists an empty session)
        # and connect to remote servers. An unknown toolset name, a missing entry agent, or a delegation
        # cycle therefore fails naming the offending value, with nothing written and nothing connected.
        registry = validated_registry(config)
        # Built here rather than in __init__ because it needs the validated registry, and here rather
        # than after the store is opened so a collision fails before anything is written.
        commands = build_command_map(config, registry)
        undeclared_commands = undeclared_workflow_commands(config, registry)

        connections: list[ServerConnection] = []
        oauth = OAuthSettings(
            storage_dir=config.data_dir / "mcp-oauth",
            callback_host=config.mcp_oauth_callback_host,
            callback_port=config.mcp_oauth_callback_port,
        )

        # Multiple conversations live in a session store. The active conversation is the most
        # recently updated (a fresh empty one if there are none).
        store = TinyDBSessionStore(str(config.sessions_path))

        scheduler = Scheduler()
        # Construct the assistant first so the registry's builder can bind its approval gate: agents are
        # built lazily (on first get), by which point assistant._approve exists.
        assistant = cls(channel, scheduler, store, config)
        assistant._workflows = commands
        assistant._undeclared_workflow_commands = undeclared_commands
        initial_id = assistant._book.adopt_most_recent()
        assistant._mcp_servers = connections  # same list the MCP tools append to / remove from

        # Per-conversation model clients: an explicit factory wins; else the injected client backs the
        # initial conversation (single-conversation tests) and further conversations build their own;
        # else every conversation builds its own from config.
        if client_factory is not None:
            raw_factory = client_factory
        elif client is not None:

            def raw_factory(conversation_id: str, _client=client, _initial=initial_id):
                if conversation_id == _initial:
                    return _client
                return build_model_client(config, entry_agent_system_message(config, state), config.entry_agent)
        else:

            def raw_factory(conversation_id: str):
                return build_model_client(config, entry_agent_system_message(config, state), config.entry_agent)

        assistant._settings.set_client_factory(raw_factory)

        # Fan a global tool mutation (MCP add/remove) out across every live conversation's agent. Reads
        # the registry lazily: it is set just below and only ever called at runtime (add/remove) or by the
        # boot reconnect, by which point the registry exists and is populated.
        def for_each_agent(apply: Callable[[object], None]) -> None:
            for agent in assistant._registry.live_agents():
                apply(agent)

        # Every toolset's shared live state, built once here (the composition root) rather than threaded
        # through build.py's functions by hand.
        state = LiveState(
            config=config,
            notify=channel.send,
            oauth=oauth,
            connections=connections,
            scheduler=scheduler,
            proactive=assistant._proactive,
            conversation_book=assistant._book,
            turn_running=assistant.turn_running,
            stop_task_runs=assistant.stop_task_runs,
            tool_approval=assistant._approve,
            reapply_config=assistant._settings.apply_one,
            observer=assistant._subagent_reporter,
            registry=registry,
            settings_table=assistant._settings_table,
        )
        # Assigned after construction because it closes over assistant._registry, which is built below it.
        state.for_each_agent = for_each_agent
        assistant._state = state
        assistant._tasks = state.tasks
        # The workflow context needs the shared toolset state, which does not exist until here.
        assistant._turns.state = state

        assistant._registry = AgentRegistry(
            make_agent_builder(
                config,
                state,
                client_factory=lambda cid: assistant._settings.client_factory(cid),
                store=store,
                images_path=config.images_path,
            ),
            cap=config.agent_cache_cap,
        )
        assistant._book.bind_registry(assistant._registry)

        # Reconnect MCP servers BEFORE building the first agent, so `connections` is populated when that
        # agent is built: the entry agent's spawn_subagent snapshots `connections` at build time to give
        # MCP-backed workers their tools. The fan-out is a no-op here (no agents are live yet); it just
        # fills `connections`.
        await reconnect_mcp_servers(for_each_agent, connections, config, notify=channel.send, oauth=oauth)
        for name in configured_but_undeclared(config):
            logger.warning(
                "config.toml has a [%s] section, but no agent declares the %r toolset, so its settings are "
                "read only if a composed worker builds it, and any command it offers does not exist. "
                "Add %r to [agents.%s].tools.",
                name,
                name,
                name,
                config.entry_agent,
            )

        # Build the active conversation's agent.
        entry_agent = assistant._registry.get(assistant._active_id)
        # Last of the startup checks, because it is the first point where every tool this config builds
        # exists: the entry agent's own, and each worker's, built when the delegation tool above was.
        validate_confirm_tools(config, state, entry_agent)

        state.tasks.arm_all()
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
    def _memory_store(self):
        """The shared semantic memory store, or None when no agent declared the memory toolset.

        Read through ``LiveState`` rather than cached here, so merely asking whether a store exists does
        not force it into existence."""
        return self._state.__dict__.get("memory_store") if self._state else None

    @property
    def _document_store(self):
        """The shared document store, or None when no agent declared the documents toolset.

        Read through ``LiveState`` rather than cached here, so merely asking whether a store exists does
        not force it into existence."""
        return self._state.__dict__.get("document_store") if self._state else None

    @property
    def history(self) -> list[dict]:
        """The active conversation's messages (OpenAI-format), for a front end to display."""
        return self._session.messages

    @property
    def history_metadata(self) -> dict:
        """The active conversation's metadata (e.g. the ``subagent`` map), for replay display."""
        return self._session.metadata

    def list_conversations(self) -> list[dict]:
        """All conversations as {id, title, updated_at, active, task_id, running}, most-recently-updated
        first.

        ``running`` is decorated on here rather than reported by ``ConversationBook.list`` because the
        book has no view of turn bookkeeping and does not need one: which turns are in flight is this
        object's own state, and the two readers of the flag (the page's spinner and the task panel's Stop
        button) both reach the list through here.
        """
        return [{**item, "running": self._tracker.running(item["id"])} for item in self._book.list()]

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

    def stop_task_runs(self, task_id: str) -> tuple[int, bool]:
        """Cancel every in-flight firing of ``task_id``; returns (how many were cancelled, whether one
        of them was the run this call is being made from).

        The mechanism behind ``TaskService.stop``, injected there the way ``_proactive`` is: which runs
        are in flight is the tracker's knowledge, and the tracker lives here. The task's schedule is
        untouched -- stopping a run is not disabling a task.

        A firing is never cancelled from inside itself. A task whose own prompt leads the model to stop it
        would otherwise cut its turn off mid-tool-call, leaving a transcript that reads like a crash and
        no room to say why, so the skip is reported back for the caller to explain instead.
        """
        current = streaming_conversation.get()
        stopped = 0
        skipped_self = False
        for conversation_id, info in self._tracker.for_task(task_id):
            if conversation_id == current:
                skipped_self = True
                continue
            info.handle.cancel()
            stopped += 1
        return stopped, skipped_self

    def _cancel_turn(self, conversation_id: str) -> None:
        """Cancel a conversation's in-flight turn without awaiting it."""
        info = self._tracker.get(conversation_id)
        if info is not None and not info.handle.done:
            info.handle.cancel()

    def current_settings(self) -> dict:
        """The effective runtime settings, in the wire shape a settings client reads."""
        return self._settings.current()

    async def apply_settings(self, incoming: dict) -> None:
        """Apply an incoming settings payload at runtime and persist it to config.toml."""
        await self._settings.apply_and_persist(incoming)

    def list_tasks(self) -> list[dict]:
        """The scheduled tasks as fields, for a front end that renders its own task rows."""
        return self._tasks.list()

    def task_action(self, action: str, name: str) -> None:
        """Run one task lifecycle action on behalf of a front end.

        The action name reaches this from a front end, so it is looked up in a table rather than
        dispatched on the string. Raises ``ValueError`` for anything not in it, and ``TaskError`` for a
        name that does not resolve. Nothing is returned: a front end shows the refreshed task list,
        not a sentence, and the sentences the tools return are written for a model.
        """
        actions: dict[str, Callable[[], object]] = {
            "enable": lambda: self._tasks.set_enabled(name, True),
            "disable": lambda: self._tasks.set_enabled(name, False),
            "run": lambda: self._tasks.run_now(name),
            "stop": lambda: self._tasks.stop(name),
            "delete": lambda: self._tasks.cancel(name),
        }
        run = actions.get(action)
        if run is None:
            raise ValueError(f"unknown task action {action!r}")
        run()

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
            # Cancel every turn still running at shutdown and let the cancellations settle (each turn
            # persists its partial state on stop), so no task is left pending. Read from `live()`, not
            # from the per-conversation entries: a turn submitted while another was running on its
            # conversation holds no entry, and one left running here is cancelled by the event loop
            # after the `close()` below, part way through the record it makes on its way down.
            turns = self._tracker.live()
            for handle in turns:
                handle.cancel()
            if turns:
                await asyncio.gather(*(handle.task for handle in turns), return_exceptions=True)
            for conn in self._mcp_servers:
                try:
                    await conn.client.aclose()
                except Exception:
                    logger.debug("Error closing MCP client", exc_info=True)
            if self._state is not None:
                self._state.close()
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
                # While an approval or a workflow decision is outstanding, the next message is its
                # answer, not a new turn. (A `/stop` above still takes priority, cancelling the waiting
                # turn.)
                if self._human.resolve_reply(raw, text):
                    continue
                # A workflow command runs this one turn through that workflow (the web UI's Plan
                # toggle sends "/plan <task>", which is exactly this path). Any other message runs a
                # plain turn. Which commands exist follows from what the entry agent declares, so a
                # capability the config did not grant has no command here.
                workflow = None
                if text.startswith("/"):
                    # word and rest come from one split, not from re-slicing raw by len(word): that
                    # used to assume exactly one character (the slash) precedes the word, but split()
                    # (unlike partition(" ")) also skips any whitespace *before* the word, so a length-
                    # based slice undercounted whenever a user typed more than one space after the
                    # slash -- e.g. "/  plan hello" ran the workflow on "an hello".
                    parts = raw.strip()[1:].split(maxsplit=1)
                    word = parts[0].lower() if parts else ""
                    candidate = self._workflows.get(word)
                    if candidate is not None:
                        task = parts[1].strip() if len(parts) > 1 else ""
                        if not task:
                            await self._ui.send(f"Usage: {candidate.usage}")
                            continue
                        msg = replace(msg, text=task)
                        workflow = candidate
                    elif word in self._undeclared_workflow_commands:
                        # An installed toolset offers /word, just not to this entry agent -- e.g. a
                        # config that predates naming "planning" in [agents.<entry>].tools. Answered
                        # here instead of falling through to a plain turn, which would otherwise run
                        # the model on the literal "/word <task>" string and persist that into history.
                        # Deliberately narrow to a word some installed workflow actually offers: a
                        # "/"-word nothing offers still falls through below, since a bare first-word
                        # heuristic would also swallow an ordinary message that starts with a path, like
                        # "/usr/local/bin is missing".
                        toolset_name = self._undeclared_workflow_commands[word]
                        await self._ui.send(
                            f"The {toolset_name!r} toolset offers /{word}, but no agent declares it. "
                            f"Add {toolset_name!r} to [agents.{self._config.entry_agent}].tools in "
                            "your config.toml."
                        )
                        continue
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
                handle = RunHandle.start(self._handle(msg, conversation_id=conversation_id, workflow=workflow, tid=tid))
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
        self, msg: ChannelMessage, *, conversation_id: str, workflow=None, tid: Optional[int] = None
    ) -> None:
        await self._turns.reactive(msg, conversation_id=conversation_id, workflow=workflow, tid=tid)

    def _diag_report(self) -> str:
        return diag_report(
            self._tracker,
            self._gate,
            config=self._config,
            entry_model=model_label(self._config, self._config.entry_agent),
            pending_approval=self._human.approval.pending,
            pending_decision=self._human.decision.pending,
        )

    async def _proactive(
        self,
        prompt: str,
        *,
        task_name: Optional[str] = None,
        task_id: Optional[str] = None,
        max_conversations: int = 0,
    ) -> None:
        await self._turns.proactive(prompt, task_name=task_name, task_id=task_id, max_conversations=max_conversations)
