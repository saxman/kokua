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

from . import runtime_settings
from .agent_registry import AgentRegistry
from .build import (
    ModelClientError,
    build_memory,
    build_model_client,
    make_agent_builder,
    resolve_system_message,
)
from .planning import PlanResult, PlanRunner
from .config import AssistantConfig
from .errors import describe_error
from .mcp import ServerConnection, reconnect_mcp_servers
from .messages import compact_message_images, derive_title
from .scheduling import make_scheduler_tools
from .turn_gate import TurnGate
from .turn_registry import TurnInfo, TurnTracker

logger = logging.getLogger(__name__)

# Re-exported so front ends can keep catching `assistant.ModelClientError` (build-time, from build) and
# `assistant.ModelConnectionError` (runtime server-unreachable, from AIMU).
__all__ = ["Assistant", "ModelClientError", "ModelConnectionError"]


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
        channel: Channel,
        scheduler: Scheduler,
        store: TinyDBSessionStore,
        config: AssistantConfig,
    ):
        self._channel = channel
        self._scheduler = scheduler
        self._store = store
        self._config = config
        # A per-conversation agent cache and the active conversation's id (replacing the single shared
        # agent + swapped-in session). Assigned by create() once the registry's builder can bind
        # self._approve; _agent / _session are read-only views onto these (see the properties below).
        self._registry: Optional[AgentRegistry] = None
        self._active_id: str = ""
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
        # Each reactive turn runs as a background task (a RunHandle) so the serve loop stays free to
        # receive a `/stop` while a turn is in flight. Tracks at most one running turn per conversation
        # (the gate enforces that invariant); backs /stop, /diag, and shutdown cancellation.
        self._tracker = TurnTracker()
        # A per-turn sequence id for the lifecycle log lines.
        self._turn_seq: int = 0
        # Tool-approval coordination. At most one approval is pending at a time (enforced by
        # self._approval_lock in _approve, not the turn gate); the serve loop resolves the future
        # with the user's answer.
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
        # The active model client's provider built-in generate kwargs, snapshotted before any override
        # is layered on, so a settings change (or a cleared field) can rebuild from a clean base.
        # Assigned by create() and refreshed on a runtime model switch.
        self._base_generate_kwargs: dict = {}
        # The current runtime generate-kwargs override (what the settings panel last set), layered over
        # config.generation on every client the factory builds. Assigned by create(), updated by
        # apply_settings, so a conversation built at any time carries the same effective kwargs as the
        # active agent.
        self._runtime_generate_kwargs: dict = {}

    @classmethod
    async def create(
        cls, config: AssistantConfig, channel: Channel, *, client=None, client_factory=None
    ) -> "Assistant":
        # Runtime-mutable settings the web panel persisted: generation kwargs, display prefs, and the
        # active model. Layered over config.toml (which is never rewritten); see runtime_settings.
        stored = runtime_settings.load(config.runtime_settings_path)
        _apply_show_flags(channel, config, stored)
        for flag in (
            "plan_review",
            "plan_review_agent",
            "result_review",
            "show_reasoning",
        ):  # config-only toggles
            if flag in stored:
                setattr(config, flag, stored[flag])

        memory_store, document_store, memory_tools = build_memory(config)

        connections: list[ServerConnection] = []
        oauth_storage_dir = config.data_dir / "mcp-oauth"

        # Multiple conversations live in a session store. The active conversation is the most
        # recently updated (a fresh empty one if there are none).
        store = TinyDBSessionStore(str(config.sessions_path))
        session = _active_session(store)

        scheduler = Scheduler()
        # Construct the assistant first so the registry's builder can bind its approval gate: agents are
        # built lazily (on first get), by which point assistant._approve exists.
        assistant = cls(channel, scheduler, store, config)
        assistant._mcp_servers = connections  # same list the MCP tools append to / remove from
        assistant._memory_store = memory_store
        assistant._document_store = document_store
        assistant._active_id = session.key
        assistant._runtime_generate_kwargs = stored.get("generate_kwargs", {})

        # Per-conversation model clients: an explicit factory wins; else the injected client backs the
        # initial conversation (single-conversation tests) and further conversations build their own;
        # else every conversation builds its own from config.
        if client_factory is not None:
            raw_factory = client_factory
        elif client is not None:
            initial_id = session.key

            def raw_factory(conversation_id: str, _client=client, _initial=initial_id):
                return _client if conversation_id == _initial else build_model_client(config, stored)
        else:

            def raw_factory(conversation_id: str):
                return build_model_client(config, stored)

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
            ),
            cap=config.agent_cache_cap,
        )

        # Build the active conversation's agent (its client is layered by the factory, which also
        # snapshots the provider base into _base_generate_kwargs) and attach any MCP servers to it,
        # preserving single-agent parity for this phase.
        assistant._registry.get(assistant._active_id)
        await reconnect_mcp_servers(
            for_each_agent, connections, config, notify=channel.send, oauth_storage_dir=oauth_storage_dir
        )

        arm_tasks()
        return assistant

    def _make_layered_factory(self, raw_factory: Callable[[str], object]) -> Callable[[str], object]:
        """Wrap a raw client factory so every built client carries the same effective generation kwargs
        the active agent has: provider defaults < config.generation < the current runtime override.

        Also snapshots the provider built-in defaults into ``_base_generate_kwargs`` (used to re-layer
        already-live agents on a settings change). Every client the factory returns is the current
        model, so that base is stable across conversations.
        """

        def build(conversation_id: str):
            client = raw_factory(conversation_id)
            base = dict(client.default_generate_kwargs)
            self._base_generate_kwargs = base
            _layer_generate_kwargs(client, base, self._config, self._runtime_generate_kwargs)
            return client

        return build

    @property
    def _agent(self) -> aio.SkillAgent:
        """The active conversation's agent (built on demand by the registry)."""
        return self._registry.get(self._active_id)

    @property
    def _session(self) -> Session:
        """The active conversation's persisted session (fetched fresh each access)."""
        return self._store.get(self._active_id)

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
                    "active": key == self._active_id,
                }
            )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return items

    async def _cancel_current_turn(self) -> None:
        """Cancel the viewed conversation's in-flight turn (if any) and let it settle, so its partial
        state persists to the conversation it belongs to before we switch away."""
        info = self._tracker.get(self._active_id)
        if info is not None and not info.handle.done:
            info.handle.cancel()
            try:
                await info.handle.task
            except Exception:
                pass

    async def new_conversation(self) -> str:
        """Start and switch to a new, empty conversation; returns its id.

        If the new conversation's agent fails to build (``ModelClientError``), the active pointer
        reverts to the previous conversation before re-raising, so the caller is never left active on
        a conversation whose agent doesn't work. The new session record itself still lingers in the
        store, unused but harmless (mirrors an ordinary empty conversation the user never sent to).
        """
        await self._cancel_current_turn()
        previous_id = self._active_id
        now = datetime.now().isoformat()
        session = Session(key=uuid.uuid4().hex, metadata={"created_at": now, "updated_at": now})
        self._store.save(session)
        self._active_id = session.key
        try:
            self._registry.get(self._active_id)  # build the (empty) agent eagerly so it is the live one
        except Exception:
            self._active_id = previous_id
            raise
        return session.key

    async def select_conversation(self, conversation_id: str) -> None:
        """Switch the active conversation to an existing one; its agent (re)builds from the store.

        If the build fails, the active pointer reverts to the previous conversation before
        re-raising, so the caller is never left active on a conversation whose agent doesn't work.
        """
        await self._cancel_current_turn()
        previous_id = self._active_id
        self._active_id = conversation_id
        try:
            self._registry.get(self._active_id)
        except Exception:
            self._active_id = previous_id
            raise

    async def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation. If it is the active one, switch to the most-recently-updated
        remaining conversation (or a fresh empty one if none remain).

        If that replacement's agent fails to build, the active pointer reverts to the just-deleted
        id before re-raising. Its store record and registry entry are already gone by that point (the
        delete itself is not rolled back), so this is a best-effort revert: it keeps ``_active_id``
        from pointing at some OTHER untested conversation, but a caller that touches ``self._agent``
        afterward will hit the same build failure again. The front end is expected to surface the
        re-raised error and stop, not retry immediately.
        """
        deleting_active = conversation_id == self._active_id
        if deleting_active:
            await self._cancel_current_turn()
        previous_id = self._active_id
        async with self._gate.exclusive():
            self._store.delete(conversation_id)
            self._registry.discard(conversation_id)
            if deleting_active:
                self._active_id = _active_session(self._store).key
        if deleting_active:
            try:
                self._registry.get(self._active_id)
            except Exception:
                self._active_id = previous_id
                raise

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

        Generation-kwargs and display-pref changes are applied in place under an exclusive gate hold
        (waits for in-flight turns to drain, blocks new ones). Switching the model rebuilds the model
        client (mirroring select_conversation: cancel the in-flight turn, then restore conversation
        state onto the new client). A model that fails to build leaves the running client untouched.
        """
        settings = runtime_settings.sanitize(incoming)
        new_model = settings.get("model")
        switching = bool(new_model) and new_model != (str(self._config.model) if self._config.model else "")

        if switching:
            await self._cancel_current_turn()
        async with self._gate.exclusive():
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
            self._runtime_generate_kwargs = settings["generate_kwargs"]
            for agent in self._registry.live_agents():
                _layer_generate_kwargs(
                    agent.model_client, self._base_generate_kwargs, self._config, self._runtime_generate_kwargs
                )
            runtime_settings.save(self._config.runtime_settings_path, settings)

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
        self._client_factory = self._make_layered_factory(lambda cid: build_model_client(self._config, {}))

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
            async for msg in self._channel.receive():
                raw = msg.text or ""
                text = raw.strip().lower()
                if text == "/stop":
                    self._stop_active_turn()
                    continue
                # /diag reports live state (and the wedged turn's async stack) without touching the
                # turn gate, so it still answers when a hung turn is holding it. Handled here, like /stop.
                if text == "/diag":
                    await self._channel.send(self._diag_report())
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
                handle.task.add_done_callback(lambda _t, cid=conversation_id: self._tracker.remove(cid))
        finally:
            self._scheduler.stop()  # channel closed -> stop the scheduler so run() returns

    def _stop_active_turn(self) -> None:
        """Cancel the viewed conversation's tracked turn (if any); the /stop branch's helper."""
        info = self._tracker.get(self._active_id)
        if info is not None and not info.handle.done:
            info.handle.cancel()

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
        try:
            async with self._gate.turn(conversation_id):
                logger.info("turn %s gate entered (%s)", tid, conversation_id)
                try:
                    if do_plan:
                        runner = PlanRunner(agent, self._channel, self._config, self._review_plan)
                        self._apply_plan_result(await runner.run(msg), conversation_id)
                    else:
                        stream = await agent.run(msg.text, stream=True, images=msg.images)
                        await self._channel.send(stream, reply_to=msg)
                except asyncio.CancelledError:
                    # `/stop` (or shutdown) cancelled this turn. Note it, keep the partial state (the
                    # agent snapshots it in a finally), and return so the daemon keeps serving.
                    logger.info("turn %s cancelled after %.1fs", tid, time.monotonic() - started)
                    try:
                        await self._channel.send("(stopped)", reply_to=msg)
                    except Exception:
                        pass
                    if self._persist(conversation_id):
                        await self._maybe_push_conversations()
                    return
                except ModelConnectionError as exc:
                    logger.exception("turn %s connection error after %.1fs", tid, time.monotonic() - started)
                    await self._channel.send(
                        f"The request couldn't reach the model server: {describe_error(exc)}", reply_to=msg
                    )
                except Exception as exc:
                    logger.exception("turn %s error after %.1fs", tid, time.monotonic() - started)
                    await self._channel.send(f"Sorry, the request failed: {describe_error(exc)}", reply_to=msg)
                else:
                    logger.info("turn %s done after %.1fs", tid, time.monotonic() - started)
                if self._persist(conversation_id):
                    await self._maybe_push_conversations()
        finally:
            self._registry.unpin(conversation_id)

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
        approval = self._pending_approval is not None and not self._pending_approval.done()
        plan = self._pending_plan is not None and not self._pending_plan.done()
        lines.append(
            f"- pending approval: {'yes' if approval else 'no'} | "
            f"pending plan: {'yes' if plan else 'no'} | proactive: {'yes' if self._in_proactive else 'no'}"
        )
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
        concerns = "\n".join(f"- {i}" for i in critique) if critique else None
        request = getattr(self._channel, "send_plan_review_request", None)
        if request is not None:
            await request(plan_text, concerns)
        else:
            note = ("\nReviewer's concerns:\n" + concerns) if concerns else ""
            await self._channel.send("[plan] Reply 'approve', 'reject', or 'edit: <revised plan>'." + note)

    async def _proactive(self, prompt: str, *, new_session: bool = False, task_name: Optional[str] = None) -> None:
        """Run an unprompted turn with ``prompt`` and surface the reply.

        The substrate for scheduled tasks: a caller (the scheduler) fires this with the task's
        instruction. Gated tools auto-deny while it runs (see ``_approve``), since no user is present
        to approve them. With ``new_session`` set (and a multi-conversation channel), the turn runs in
        a fresh conversation and the user's active conversation is restored afterward, so a scheduled
        run does not hijack whatever the user is currently viewing.
        """
        multi_conversation = getattr(self._channel, "send_conversations", None) is not None
        # Each branch takes at most one gate hold, never both: the new-session branch's work happens
        # entirely on the new conversation (inside _run_in_new_session's own gate.turn(new_id)), and
        # the non-new-session branch's work happens on the viewed conversation (gate.turn(self._active_id)
        # here). Nesting an outer hold on self._active_id around a call that acquires a *different*
        # conversation's hold was a latent deadlock: with the gate's writer-preference, a concurrent
        # exclusive() (e.g. a settings change) can see this task's outer reader stuck waiting to
        # re-enter as an inner reader on the new conversation, while the writer itself waits for that
        # outer reader to drop to zero -- neither side can proceed.
        self._in_proactive = True
        try:
            if new_session and multi_conversation:
                await self._run_in_new_session(prompt, task_name)
            else:
                async with self._gate.turn(self._active_id):
                    # Tag every message this unprompted run appends so replayed history can distinguish
                    # it from a user-driven turn. The agent doesn't reset on run (system prompt lives on
                    # the client), so the pre-run length is a stable start index for the exchange.
                    start = len(self._agent.model_client.messages)
                    reply = await self._agent.run(prompt)
                    for message in self._agent.model_client.messages[start:]:
                        message[PROVENANCE_KEY] = PROVENANCE_PROACTIVE
                    await self._channel.send(reply)
                    if self._persist(self._active_id):
                        await self._maybe_push_conversations()
        except ModelConnectionError as exc:
            # Surface the reason and swallow it: a scheduled turn has no user awaiting, and letting it
            # propagate would crash the scheduler task (`_fire_job` has no except).
            logger.exception("proactive turn connection error")
            await self._channel.send(f"A scheduled task couldn't reach the model server: {describe_error(exc)}")
        except Exception as exc:
            logger.exception("proactive turn error")
            await self._channel.send(f"A scheduled task failed: {describe_error(exc)}")
        finally:
            self._in_proactive = False

    async def _run_in_new_session(self, prompt: str, task_name: Optional[str]) -> None:
        """Run a proactive turn in a fresh conversation, then restore the active one.

        The caller (``_proactive``) takes no gate hold of its own for this branch; this acquires the
        only gate hold for the call, on the new conversation's id, around the actual run. Switching
        conversations is just an active-id swap (each conversation has its own agent), restored in a
        ``finally`` so the user's active conversation is never left pointing at the task's session.
        """
        previous_id = self._active_id
        now = datetime.now().isoformat()
        title = task_name or derive_title([{"role": "user", "content": prompt}]) or "Scheduled task"
        session = Session(key=uuid.uuid4().hex, metadata={"created_at": now, "updated_at": now, "title": title})
        self._store.save(session)
        self._active_id = session.key
        try:
            async with self._gate.turn(session.key):
                agent = self._registry.get(self._active_id)
                start = len(agent.model_client.messages)
                await agent.run(prompt)
                for message in agent.model_client.messages[start:]:
                    message[PROVENANCE_KEY] = PROVENANCE_PROACTIVE
                self._persist(session.key)
        finally:
            self._active_id = previous_id
        try:
            await self._maybe_push_conversations()
            await self._channel.send(f"Scheduled task '{title}' finished; open the '{title}' conversation to review.")
        except Exception:
            logger.warning("Scheduled task '%s' ran; its notification could not be delivered", title, exc_info=True)

    def _apply_plan_result(self, result: "PlanResult", conversation_id: str) -> None:
        """Record a planned turn's reviewer verdicts and verbose trace under the turn's user-message
        index, so reload replays them. Loads the session by id, mutates its metadata, and saves.
        No-op when the turn did not commit (e.g. plan rejected)."""
        if not result.committed or result.user_index < 0:
            return
        session = self._store.get(conversation_id)
        changed = False
        if result.subagent_events:
            session.metadata.setdefault("subagent", {})[str(result.user_index)] = result.subagent_events
            changed = True
        if result.trace:
            session.metadata.setdefault("trace", {})[str(result.user_index)] = result.trace
            changed = True
        if changed:
            self._store.save(session)

    def _persist(self, conversation_id: str) -> bool:
        """Snapshot conversation_id's agent messages onto its session and save. Returns True if a
        title was just derived (first user message), so a caller can refresh the conversation list."""
        session = self._store.get(conversation_id)
        agent = self._registry.get(conversation_id)
        session.messages = compact_message_images(
            [dict(m) for m in agent.model_client.messages], self._config.images_path
        )
        title_set = False
        if not session.metadata.get("title"):
            title = derive_title(session.messages)
            if title:
                session.metadata["title"] = title
                title_set = True
        session.metadata["updated_at"] = datetime.now().isoformat()
        self._store.save(session)
        return title_set
