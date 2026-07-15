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
from dataclasses import replace
from datetime import datetime
from typing import Optional

from aimu import PROVENANCE_KEY, PROVENANCE_PROACTIVE, aio
from aimu.aio import Channel, RunHandle, Scheduler
from aimu.aio.channels.base import ChannelMessage
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.sessions import Session, TinyDBSessionStore

from . import runtime_settings
from .build import (
    ModelClientError,
    add_subagent_tool,
    build_agent,
    build_memory,
    build_model_client,
    resolve_system_message,
)
from .planning import PlanResult, PlanRunner
from .config import AssistantConfig
from .mcp import ServerConnection, reconnect_mcp_servers
from .messages import compact_message_images, derive_title, expand_message_images

logger = logging.getLogger(__name__)

# Re-exported so front ends can keep catching `assistant.ModelClientError`; the class lives in build.
__all__ = ["Assistant", "ModelClientError"]


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
        self._mcp_servers: list[ServerConnection] = []
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
        # Tool-approval coordination. At most one approval is pending at a time (enforced by
        # self._approval_lock in _approve, not self._lock); the serve loop resolves the future
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

    @classmethod
    async def create(cls, config: AssistantConfig, channel: Channel, *, client=None) -> "Assistant":
        # Runtime-mutable settings the web panel persisted: generation kwargs, display prefs, and the
        # active model. Layered over config.toml (which is never rewritten); see runtime_settings.
        stored = runtime_settings.load(config.runtime_settings_path)
        if client is None:
            client = build_model_client(config, stored)

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

        memory_store, document_store, memory_tools = build_memory(config)

        connections: list[ServerConnection] = []
        oauth_storage_dir = config.data_dir / "mcp-oauth"
        agent = build_agent(
            config,
            client,
            notify=channel.send,
            oauth_storage_dir=oauth_storage_dir,
            connections=connections,
            memory_tools=memory_tools,
        )
        await reconnect_mcp_servers(
            agent, connections, config, notify=channel.send, oauth_storage_dir=oauth_storage_dir
        )

        # Multiple conversations live in a session store. The active conversation is the most
        # recently updated (a fresh empty one if there are none).
        store = TinyDBSessionStore(str(config.sessions_path))
        session = _active_session(store)
        if session.messages:
            agent.restore(expand_message_images(session.messages, config.images_path))

        scheduler = Scheduler()
        assistant = cls(agent, channel, scheduler, store, session, config)
        assistant._mcp_servers = connections  # same list the MCP tools append to / remove from
        assistant._memory_store = memory_store
        assistant._document_store = document_store
        assistant._base_generate_kwargs = base_generate_kwargs
        # Gate configured "risky" tools behind interactive approval (see _approve). Published to the
        # model client on every run by the agent's _prepare_run; an empty confirm_tools is a no-op.
        agent.tool_approval = assistant._approve
        add_subagent_tool(agent, config, assistant._approve)
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
            self._agent.restore(expand_message_images(session.messages, self._config.images_path))
        return session.key

    async def select_conversation(self, conversation_id: str) -> None:
        """Switch the active conversation to an existing one and restore it into the agent."""
        await self._cancel_current_turn()
        async with self._lock:
            self._session = self._store.get(conversation_id)
            self._agent.restore(expand_message_images(self._session.messages, self._config.images_path))

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
                self._agent.restore(expand_message_images(self._session.messages, self._config.images_path))

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
        new_client = aio.client(model, system=resolve_system_message(self._config))  # build first; only swap on success
        self._agent.model_client = new_client
        self._agent.restore(expand_message_images(self._session.messages, self._config.images_path))
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
                    runner = PlanRunner(self._agent, self._channel, self._config, self._review_plan)
                    self._apply_plan_result(await runner.run(msg))
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

    def _apply_plan_result(self, result: "PlanResult") -> None:
        """Record a planned turn's reviewer verdicts and verbose trace under the turn's user-message
        index, so reload replays them. Persisted by _persist (which saves session.metadata). No-op when
        the turn did not commit (e.g. plan rejected)."""
        if not result.committed or result.user_index < 0:
            return
        if result.subagent_events:
            self._session.metadata.setdefault("subagent", {})[str(result.user_index)] = result.subagent_events
        if result.trace:
            self._session.metadata.setdefault("trace", {})[str(result.user_index)] = result.trace

    def _persist(self) -> bool:
        """Snapshot the agent's messages onto the active session and save. Returns True if a title
        was just derived (first user message), so a caller can refresh the conversation list."""
        messages = compact_message_images(
            [dict(m) for m in self._agent.model_client.messages], self._config.images_path
        )
        self._session.messages = messages
        title_set = False
        if not self._session.metadata.get("title"):
            title = derive_title(messages)
            if title:
                self._session.metadata["title"] = title
                title_set = True
        self._session.metadata["updated_at"] = datetime.now().isoformat()
        self._store.save(self._session)
        return title_set
