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
import io
import logging
import time
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional

from aimu import PROVENANCE_KEY, PROVENANCE_PROACTIVE, aio
from aimu.aio import Channel, ModelConnectionError, RunHandle, Scheduler
from aimu.aio.channels.base import ChannelMessage
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.sessions import Session, TinyDBSessionStore

from . import config_store, runtime_settings
from .agent_registry import AgentRegistry
from .channels.ui import ChannelUI
from .channels.web import proactive_turn, streaming_conversation
from .build import (
    ModelClientError,
    build_memory,
    build_model_client,
    make_agent_builder,
    resolve_system_message,
)
from .planning import PlanResult, PlanRunner
from .config import AssistantConfig
from .conversations import ConversationBook
from .errors import describe_error
from .interaction import HumanGate
from .mcp import ServerConnection, reconnect_mcp_servers
from .messages import derive_title
from .scheduling import make_scheduler_tools
from .turn_gate import TurnGate
from .turn_registry import TurnInfo, TurnTracker

logger = logging.getLogger(__name__)

# Re-exported so front ends can keep catching `assistant.ModelClientError` (build-time, from build) and
# `assistant.ModelConnectionError` (runtime server-unreachable, from AIMU).
__all__ = ["Assistant", "ModelClientError", "ModelConnectionError"]


def _layer_generate_kwargs(client, base: dict, config: AssistantConfig) -> None:
    """Rebuild the client's default generate kwargs in place from the config's generation settings.

    Order (later wins): provider built-in defaults (`base`) < config.toml `[generation]`. The settings
    panel and update_config now write straight into `config.generation`, so it is the single effective
    layer; a key it never set (e.g. presence_penalty on Anthropic) is never injected.
    """
    kwargs = client.default_generate_kwargs
    kwargs.clear()
    kwargs.update(base)
    kwargs.update(config.generation)


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
        self._scheduler = scheduler
        self._store = store
        self._config = config
        # A per-conversation agent cache. Assigned by create() once the registry's builder can bind
        # self._approve; agents are built lazily, by which point it exists.
        self._registry: Optional[AgentRegistry] = None
        self._client_factory = None
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
        # The active model client's provider built-in generate kwargs, snapshotted before any override
        # is layered on, so a settings change (or a cleared field) can rebuild from a clean base.
        # Assigned by create() and refreshed on a runtime model switch.
        self._base_generate_kwargs: dict = {}

    @classmethod
    async def create(
        cls, config: AssistantConfig, channel: Channel, *, client=None, client_factory=None
    ) -> "Assistant":
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
        assistant._client_factory = assistant._make_layered_factory(raw_factory)
        scheduler_tools, arm_tasks = make_scheduler_tools(scheduler, config.scheduled_tasks_path, assistant._proactive)

        # Fan a global tool mutation (MCP add/remove) out across every live conversation's agent. Reads
        # the registry lazily: it is set just below and only ever called at runtime (add/remove) or by the
        # boot reconnect, by which point the registry exists and is populated.
        def for_each_agent(apply: Callable[[object], None]) -> None:
            for agent in assistant._registry.live_agents():
                apply(agent)

        assistant._registry = AgentRegistry(
            make_agent_builder(
                config,
                client_factory=lambda cid: assistant._client_factory(cid),
                notify=channel.send,
                oauth_storage_dir=oauth_storage_dir,
                connections=connections,
                memory_tools=memory_tools,
                tool_approval=assistant._approve,
                scheduler_tools=scheduler_tools,
                store=store,
                images_path=config.images_path,
                for_each_agent=for_each_agent,
                reapply_config=assistant._apply_config_change,
            ),
            cap=config.agent_cache_cap,
        )
        assistant._book.bind_registry(assistant._registry)

        # Reconnect MCP servers BEFORE building the first agent, so `connections` is populated when
        # that agent is built: a flat agent attaches the servers' tools directly, and a lean
        # supervisor's spawn_subagent snapshots `connections` at build time to give MCP-backed workers
        # their tools. The fan-out is a no-op here (no agents are live yet); it just fills `connections`.
        await reconnect_mcp_servers(
            for_each_agent, connections, config, notify=channel.send, oauth_storage_dir=oauth_storage_dir
        )
        if config.lean_supervisor and not config.subagents:
            logger.warning("lean_supervisor is set but subagents is off; using the flat toolset instead.")

        # Build the active conversation's agent (its client is layered by the factory, which also
        # snapshots the provider base into _base_generate_kwargs).
        assistant._registry.get(assistant._active_id)

        arm_tasks()
        return assistant

    def _make_layered_factory(self, raw_factory: Callable[[str], object]) -> Callable[[str], object]:
        """Wrap a raw client factory so every built client carries the same effective generation kwargs
        the active agent has: provider defaults < config.generation.

        Also snapshots the provider built-in defaults into ``_base_generate_kwargs`` (used to re-layer
        already-live agents on a settings change). Every client the factory returns is the current
        model, so that base is stable across conversations.
        """

        def build(conversation_id: str):
            client = raw_factory(conversation_id)
            base = dict(client.default_generate_kwargs)
            self._base_generate_kwargs = base
            _layer_generate_kwargs(client, base, self._config)
            return client

        return build

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
        """The effective runtime settings for the web panel to display: model, prefs, generate kwargs."""
        settings = {setting.field: self._read_setting(setting) for setting in runtime_settings.RUNTIME_SETTINGS}
        settings["generate_kwargs"] = dict(self._agent.model_client.default_generate_kwargs)
        return settings

    def _read_setting(self, setting: runtime_settings.RuntimeSetting):
        """One runtime setting's effective value: the channel's copy of a mirrored flag wins, since
        that is the one actually consulted while streaming."""
        value = getattr(self._config, setting.field)
        if setting.kind is str:
            return str(value) if value else ""
        if setting.mirror_on_channel:
            return self._ui.display_flag(setting.field, value)
        return value

    async def apply_settings(self, incoming: dict) -> None:
        """Apply a settings-panel change at runtime and persist it to config.toml so it survives restarts.

        The panel sends the full set of settings it exposes; ``_apply_settings`` applies them to the live
        session and ``_persist_settings`` writes them back to config.toml (the single source of truth).
        """
        applied = await self._apply_settings(runtime_settings.sanitize(incoming))
        self._persist_settings(applied)

    async def _apply_settings(self, settings: dict) -> dict:
        """Apply a sanitized settings dict (model, display/planning flags, generation kwargs) live.

        Generation-kwargs and display-pref changes are applied in place under an exclusive gate hold
        (waits for in-flight turns to drain, blocks new ones). Switching the model rebuilds the model
        client (mirroring select_conversation: cancel the in-flight turn, then restore conversation
        state onto the new client). A model that fails to build raises and leaves the running client
        untouched. Returns the settings that were applied (for the caller to persist).
        """
        new_model = settings.get("model")
        switching = bool(new_model) and new_model != (str(self._config.model) if self._config.model else "")

        if switching:
            await self._cancel_current_turn()
        async with self._gate.exclusive():
            if switching:
                await self._switch_model(new_model)
            for setting in runtime_settings.RUNTIME_SETTINGS:
                if setting.field not in settings or setting.field == "model":  # model: handled above
                    continue
                setattr(self._config, setting.field, settings[setting.field])
                if setting.mirror_on_channel:
                    self._ui.set_display_flag(setting.field, settings[setting.field])
            self._config.generation = settings["generate_kwargs"]
            for agent in self._registry.live_agents():
                _layer_generate_kwargs(agent.model_client, self._base_generate_kwargs, self._config)
        return settings

    def _persist_settings(self, settings: dict) -> None:
        """Write an applied settings dict back into config.toml, keeping [generation] in sync."""
        path = self._config.config_path
        for setting in runtime_settings.RUNTIME_SETTINGS:
            if setting.field in settings:
                config_store.set_value(path, setting.section, setting.toml_key, settings[setting.field])
        generate_kwargs = settings["generate_kwargs"]
        for key in runtime_settings.GENERATION_KEYS:
            if key in generate_kwargs:
                config_store.set_value(path, "generation", key, generate_kwargs[key])
            else:
                config_store.unset_value(path, "generation", key)

    async def _apply_config_change(self, section: str, key: str, value) -> None:
        """Apply one hot ``update_config`` change to the live session (no persist; the tool writes disk).

        Builds the panel-shaped settings dict for the single change (always carrying the current
        generation set so ``_apply_settings`` does not wipe it) and applies it. Raises if it cannot be
        applied (e.g. an invalid model), so the tool skips persisting a change that did not take.
        """
        applied = {"generate_kwargs": dict(self._config.generation)}
        if section == "generation":
            applied["generate_kwargs"][key] = value
        else:
            setting = runtime_settings.by_toml(section, key)
            if setting is not None:
                applied[setting.field] = value
        await self._apply_settings(runtime_settings.sanitize(applied))

    async def _switch_model(self, model: str) -> None:
        """Rebuild every live agent's client for the new model, preserving each conversation's messages.

        Tools bind the agent (not the client), so they survive; each agent's own messages are restored
        onto its new client. ``aio.client`` is called once per cached agent, with the same fixed model
        string each time, so the first call to fail means every call fails: a bad model raises before
        any agent is swapped, and no partial swap happens in practice. Also updates the client factory
        so conversations built later use the new model.
        """
        system = resolve_system_message(self._config)
        for conversation_id in self._registry.cached_ids():
            agent = self._registry.get(conversation_id)
            new_client = aio.client(model, system=system)
            messages = list(agent.model_client.messages)
            agent.model_client = new_client
            agent.restore(messages)
        self._config.model = model
        self._base_generate_kwargs = dict(self._agent.model_client.default_generate_kwargs)
        # Later-built conversations go through build_model_client (so a since-broken model raises
        # ModelClientError, not a raw ValueError/TypeError) and get the same layered generation kwargs.
        self._client_factory = self._make_layered_factory(lambda cid: build_model_client(self._config))

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
        # Planning is opt-in per turn (the web Plan toggle or a `/plan <task>` message sets plan=True).
        do_plan = plan
        started = time.monotonic()
        agent = self._registry.get(conversation_id)
        # Pinned for the whole turn so LRU eviction can't drop this conversation's agent out from
        # under an in-flight turn, even if other conversations' turns push it past the cache cap.
        self._registry.pin(conversation_id)
        # Carries this turn's conversation id for the duration of this task (and its awaited children,
        # e.g. agent.run / a gated tool's _approve call) for two readers: the web channel mutes a
        # background turn's streaming frames (only the conversation being viewed streams; see
        # channels/web.py), and _approve auto-denies a gated tool whose turn isn't the viewed
        # conversation. Set unconditionally -- even the CLI channel (single-view, never switches) needs
        # it so its turn's conversation_id reads as foreground in _approve.
        token = streaming_conversation.set(conversation_id)
        succeeded = False
        failure_reason = ""  # set on error, so a backgrounded turn's notification can carry the reason
        try:
            async with self._gate.turn(conversation_id):
                logger.info("turn %s gate entered (%s)", tid, conversation_id)
                try:
                    if do_plan:
                        runner = PlanRunner(agent, self._ui, self._config, self._human.review_plan)
                        self._apply_plan_result(await runner.run(msg), conversation_id)
                    else:
                        stream = await agent.run(msg.text, stream=True, images=msg.images)
                        await self._ui.send(stream, reply_to=msg)
                except asyncio.CancelledError:
                    # `/stop` (or shutdown) cancelled this turn. Note it, keep the partial state (the
                    # agent snapshots it in a finally), and return so the daemon keeps serving.
                    logger.info("turn %s cancelled after %.1fs", tid, time.monotonic() - started)
                    try:
                        await self._ui.send("(stopped)", reply_to=msg)
                    except Exception:
                        pass
                    if self._persist(conversation_id):
                        await self._maybe_push_conversations()
                    return
                except ModelConnectionError as exc:
                    logger.exception("turn %s connection error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"couldn't reach the model server: {describe_error(exc)}"
                    await self._ui.send(
                        f"The request couldn't reach the model server: {describe_error(exc)}", reply_to=msg
                    )
                except Exception as exc:
                    logger.exception("turn %s error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"failed: {describe_error(exc)}"
                    await self._ui.send(f"Sorry, the request failed: {describe_error(exc)}", reply_to=msg)
                else:
                    logger.info("turn %s done after %.1fs", tid, time.monotonic() - started)
                    succeeded = True
                if self._persist(conversation_id):
                    await self._maybe_push_conversations()
        finally:
            streaming_conversation.reset(token)
            self._registry.unpin(conversation_id)
        # The user switched away before this turn finished (it ran to completion in the background):
        # tell them rather than silently updating a conversation they're not looking at. The reply
        # (or the error message) went out muted, so this notification is the only signal they get.
        # A cancelled turn returns before this point, so it never notifies. On failure the reason is
        # carried in the notification because the muted error message is not persisted and so is not
        # visible on switch-in.
        if conversation_id != self._active_id:
            title = self._store.get(conversation_id).metadata.get("title") or "a conversation"
            if succeeded:
                await self._ui.notify(f"Reply ready in '{title}'.")
            else:
                await self._ui.notify(f"A reply in '{title}' {failure_reason}.")

    def _diag_report(self) -> str:
        """A snapshot of live turn/gate state for the `/diag` command, plus each wedged turn's async
        stack. Reads only in-memory state and never awaits the turn gate, so it answers even while a
        hung turn holds it (the case it exists to diagnose)."""
        turns = self._tracker.all()
        lines = ["Diagnostics:"]
        if turns:
            lines.append(f"- turn in flight: yes ({len(turns)})")
            for conversation_id, info in turns:
                elapsed = time.monotonic() - info.started
                lines.append(f"  - {conversation_id}: elapsed {elapsed:.1f}s, message: {info.preview!r}")
        else:
            lines.append("- turn in flight: no")
        lines.append(f"- active turns: {self._gate.active_turns()}")
        approval = self._human.approval.pending
        plan = self._human.plan.pending
        lines.append(f"- pending approval: {'yes' if approval else 'no'} | pending plan: {'yes' if plan else 'no'}")
        for conversation_id, info in turns:
            if info.handle.done:
                continue
            stack = self._format_task_stack(info.handle.task)
            if stack:
                lines.append(
                    f"\nStuck turn stack for {conversation_id} "
                    f"(async only; run `kill -USR1 <pid>` for full thread stacks):\n```\n{stack}\n```"
                )
        return "\n".join(lines)

    @staticmethod
    def _format_task_stack(task) -> str:
        """Render an asyncio task's current async stack in-process (a sudo-free py-spy). Best-effort:
        returns '' if the task finished or the dump fails."""
        try:
            buffer = io.StringIO()
            task.print_stack(file=buffer)
            return buffer.getvalue().strip()
        except Exception:
            return ""

    async def _proactive(
        self,
        prompt: str,
        *,
        target: str = "active",
        task_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Run an unprompted turn with ``prompt`` and surface the reply.

        The substrate for scheduled tasks: a caller (the scheduler) fires this with the task's
        instruction. ``target`` selects the conversation the turn runs in:

        - ``"active"``: the currently-viewed conversation (``self._active_id``).
        - ``"new"``: a fresh conversation minted for this firing alone.
        - ``"task"``: the task's own dedicated conversation, reused across firings -- ``session_id`` is
          the key it created previously (``None`` on the first firing).

        Returns the key of the conversation the turn ran in for ``"new"``/``"task"`` (so the caller can
        persist a ``"task"`` conversation's key), or ``None`` for ``"active"``.

        Sets ``streaming_conversation`` to this run's conversation for the duration, so ``_approve``
        gates a gated tool call the same way it would for any other turn: prompted if that conversation
        is the one ``self._active_id`` currently points at, auto-denied otherwise (no user watching to
        confirm). The ``"new"``/``"task"`` branch never touches ``self._active_id`` at all (see
        ``_run_in_new_session``), so a scheduled run never hijacks whatever the user is currently
        viewing -- its own gated tool calls always auto-deny, since that conversation is never the one
        ``self._active_id`` points at.
        """
        multi_conversation = self._ui.supports_conversations
        # Each branch takes at most one gate hold, never both: returning right after
        # _run_in_new_session leaves it as the only holder of its own gate.turn(new_id); nesting an
        # outer hold on self._active_id around that call was a latent deadlock (regression-tested by
        # test_proactive_new_session_holds_at_most_one_gate_turn) -- with the gate's writer-preference,
        # a concurrent exclusive() could see this task's outer reader stuck waiting to re-enter as an
        # inner reader on the new conversation, while the writer waits for that outer reader to drop
        # to zero, and neither side can proceed.
        if target in ("new", "task") and multi_conversation:
            # "new" always mints a fresh conversation; "task" reuses its remembered one when present.
            reuse = session_id if target == "task" else None
            return await self._run_in_new_session(prompt, task_name, session_id=reuse)
        # Captured once so the rest of this run is internally consistent even if the user switches
        # conversations while it's in flight (a switch does not cancel a running turn).
        conversation_id = self._active_id
        token = streaming_conversation.set(conversation_id)
        proactive_token = proactive_turn.set(True)  # gated tools auto-deny for the whole run (unattended)
        # Pinned for the whole run so LRU eviction can't drop this conversation's agent mid-run (which
        # would leave _persist rebuilding a stale one and losing this turn's output), mirroring _handle.
        self._registry.pin(conversation_id)
        try:
            async with self._gate.turn(conversation_id):
                agent = self._registry.get(conversation_id)
                # Tag every message this unprompted run appends so replayed history can distinguish
                # it from a user-driven turn. The agent doesn't reset on run (system prompt lives on
                # the client), so the pre-run length is a stable start index for the exchange.
                start = len(agent.model_client.messages)
                reply = await agent.run(prompt)
                for message in agent.model_client.messages[start:]:
                    message[PROVENANCE_KEY] = PROVENANCE_PROACTIVE
                await self._ui.send(reply)
                if self._persist(conversation_id):
                    await self._maybe_push_conversations()
        except ModelConnectionError as exc:
            # Surface the reason and swallow it: a scheduled turn has no user awaiting, and letting it
            # propagate would crash the scheduler task (`_fire_job` has no except).
            logger.exception("proactive turn connection error")
            await self._ui.send(f"A scheduled task couldn't reach the model server: {describe_error(exc)}")
        except Exception as exc:
            logger.exception("proactive turn error")
            await self._ui.send(f"A scheduled task failed: {describe_error(exc)}")
        finally:
            self._registry.unpin(conversation_id)
            proactive_turn.reset(proactive_token)
            streaming_conversation.reset(token)

    async def _run_in_new_session(
        self, prompt: str, task_name: Optional[str], *, session_id: Optional[str] = None
    ) -> str:
        """Run a proactive turn in a task-owned conversation without disturbing the viewed one.

        With ``session_id`` given and still present in the store, the turn reuses that conversation
        (the registry replays its history, so the task sees its prior firings); otherwise a fresh
        conversation is minted. A ``session_id`` that no longer exists is treated as absent and
        recreated -- so a task keeps working after its conversation is deleted. Existence is checked
        against ``list_keys()`` rather than ``store.get()``, because the store returns an empty
        ``Session`` for a missing key, which would otherwise resurrect a deleted conversation as a
        blank one. Returns the key of the conversation the turn actually ran in.

        The caller (``_proactive``) takes no gate hold of its own for this branch; this acquires the
        only gate hold for the call, on the conversation's id, around the actual run. Unlike
        select/new_conversation, this never touches ``self._active_id`` -- it is not "switching" to
        the session, just running a turn on it (the registry looks up any conversation's agent by
        id, no active-pointer swap needed). Leaving ``self._active_id`` alone is what keeps this turn
        consistent with every other concurrency invariant: ``streaming_conversation`` (set to
        ``session.key`` below) then never equals the viewed conversation, so ``_approve`` auto-denies a
        gated tool call here (no user is watching this session) instead of prompting; the serve loop's
        `conversation_id = self._active_id` at submit time still binds a message the user sends during
        this run to the conversation they're actually viewing, not to this one; and there is no active
        id for a concurrent user switch to race or for a ``finally`` to clobber back.
        """
        now = datetime.now().isoformat()
        if session_id and session_id in self._store.list_keys():
            session = self._store.get(session_id)
            session.metadata["updated_at"] = now
        else:
            title = task_name or derive_title([{"role": "user", "content": prompt}]) or "Scheduled task"
            session = Session(key=uuid.uuid4().hex, metadata={"created_at": now, "updated_at": now, "title": title})
        title = session.metadata.get("title") or "Scheduled task"
        self._store.save(session)
        token = streaming_conversation.set(session.key)
        proactive_token = proactive_turn.set(True)  # gated tools auto-deny for the whole run (unattended)
        # Pinned for the whole run so LRU eviction can't drop this session's agent mid-run and leave
        # _persist rebuilding a stale one, mirroring _handle and the non-new-session branch.
        self._registry.pin(session.key)
        try:
            async with self._gate.turn(session.key):
                agent = self._registry.get(session.key)
                start = len(agent.model_client.messages)
                await agent.run(prompt)
                for message in agent.model_client.messages[start:]:
                    message[PROVENANCE_KEY] = PROVENANCE_PROACTIVE
                self._persist(session.key)
        finally:
            self._registry.unpin(session.key)
            proactive_turn.reset(proactive_token)
            streaming_conversation.reset(token)
        try:
            await self._maybe_push_conversations()
            await self._ui.send(f"Scheduled task '{title}' finished; open the '{title}' conversation to review.")
        except Exception:
            logger.warning("Scheduled task '%s' ran; its notification could not be delivered", title, exc_info=True)
        return session.key

    def _apply_plan_result(self, result: "PlanResult", conversation_id: str) -> None:
        return self._book.record_plan_metadata(result, conversation_id)

    def _persist(self, conversation_id: str) -> bool:
        return self._book.persist(conversation_id)
