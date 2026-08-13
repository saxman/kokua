"""Running a turn: reactive (the user sent something) and proactive (a scheduled task fired).

## Concurrency invariants

Every rule here was learned from a bug. Read them before changing anything in this module.

1. **One gate hold per task, taken exactly once.** ``TurnGate`` is writer-preferring: a waiting
   exclusive() blocks new readers. A task that holds a reader and then tries to take a *second*
   reader deadlocks against a concurrent exclusive(), which is waiting for the reader count to reach
   zero that the outer hold will never release. Every path below takes exactly one
   ``gate.turn(...)``, and no path calls another path that takes one. Do not wrap a call to
   ``reactive`` or ``proactive`` in a gate hold.
   (Regression: ``test_proactive_new_session_holds_at_most_one_gate_turn``.)

2. **Pin for the whole turn.** The agent registry evicts LRU. Without a pin, another conversation's
   turn can evict this one's agent mid-run, and persisting afterwards would rebuild a stale agent
   from the store and silently lose this turn's output. Pin before the gate, unpin in a ``finally``.

3. **``streaming_conversation`` is set for every turn, unconditionally.** Two readers depend on it:
   the web channel mutes frames from a turn that isn't the conversation being viewed, and the
   approval gate auto-denies a gated tool whose turn isn't being watched. Even the CLI channel needs
   it set, so that its single conversation reads as foreground rather than as a background turn.
   ``subagent_events`` is installed the same way, for the same reason on the recording side: it must
   be set before a turn's first ``await`` and reset only in the ``finally`` alongside
   ``streaming_conversation``, or a spawn racing the set/reset window would report into a stale list
   (or into no list at all) instead of the turn that owns it. The channel's catch-up record is opened
   in the same place and for the same reason, but it is *ended by ``_persist``* rather than by the
   ``finally``: the store and the record stand for the same output, so ending it next to the write that
   supersedes it is what keeps a switch-in from replaying both. Every turn opens one, unattended runs
   included -- a scheduled task's spawn cards are muted like any other background turn's frames, and
   they are the only display frames such a turn produces. An unattended run opens its record *inside*
   the gate, because a firing can queue behind a turn already running on the same conversation and
   would otherwise replace that turn's record while it is still standing in for live output.
   (Regression: ``test_an_unattended_turn_opens_a_catch_up_record_for_the_conversation_it_runs_in``.)

4. **A proactive run never touches the active pointer.** It is not "switching" to a conversation,
   just running a turn on one -- the registry looks up any conversation's agent by id. Leaving the
   pointer alone is what makes everything else consistent: the run's gated tool calls auto-deny
   (nobody is watching a conversation the user isn't viewing), a message the user sends during the
   run still binds to the conversation they are actually looking at, and there is no active id for a
   concurrent switch to race or for a ``finally`` to clobber back.

5. **Record a turn's sub-agent events before its own notification send, in every branch.**
   ``_record_subagents`` is synchronous, but that only protects it from a cancellation that arrives
   *after* it runs. A cancelled or failed turn still does one more ``await`` of its own (the
   "(stopped)" notice, or the failure message) before falling through to the shared ``_persist``
   call; a second cancellation delivered during that send raises ``CancelledError`` again, which
   propagates past a record call placed after the send and drops the turn's events. Every branch --
   cancelled, connection error, generic error, success -- records as its first action, not as a step
   shared only by the paths that return normally. Recording under the right index is half of this,
   and every path resolves that index with ``resolve_user_index`` (the pre-run length is not it: a
   first turn seeds the system message ahead of the user message). A planned turn's index cannot come
   from the ``PlanResult`` either, since a cancelled or failed run raises instead of returning one, so
   ``PlanRunner`` publishes the index as it commits and the plan branch reads it back in a ``finally``.
   (Regressions: ``test_a_second_cancellation_during_the_stopped_send_still_records``,
   ``test_a_stopped_planned_turn_records_the_events_it_produced``,
   ``test_a_first_turns_cards_anchor_to_its_user_message_past_the_system_message``.)

6. **An unattended turn never lets an exception escape.** A scheduled firing has no user awaiting it
   and runs inside a scheduler job with no handler of its own, so a propagating error would take the
   scheduler down with it. Report it on the channel and swallow it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from aimu import PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.aio import ModelConnectionError
from aimu.aio.channels.base import ChannelMessage

from kokua.channels.web import proactive_turn, streaming_conversation
from kokua.core.errors import describe_error
from kokua.core.messages import derive_title, resolve_user_index
from kokua.core.subagents import subagent_events
from kokua.planning.runner import PlanRunner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProactiveTarget:
    """Which conversation an unattended run uses, and how it reports itself.

    ``"active"`` runs in the conversation being viewed and echoes the reply there, so the user sees
    it happen. ``"new"``/``"task"`` run in their own conversation, which the user is by definition
    not looking at, so instead of echoing a reply into nowhere they announce that it finished and
    return the key so the caller can find it again.
    """

    conversation_id: str
    echo_reply: bool
    announce: Optional[str] = None
    returns_key: bool = False


class TurnRunner:
    def __init__(self, book, ui, gate, config, *, review_plan, push_conversations):
        self._book = book
        self._ui = ui
        self._gate = gate
        self._config = config
        self._review_plan = review_plan
        self._push_conversations = push_conversations

    # --- reactive -------------------------------------------------------------------------------

    async def reactive(
        self, msg: ChannelMessage, *, conversation_id: str, plan: bool = False, tid: Optional[int] = None
    ) -> None:
        """Run a user-initiated turn and send its reply. See the module's concurrency invariants."""
        started = time.monotonic()
        agent = self._book.agent_for(conversation_id)
        self._book.pin(conversation_id)  # invariant 2
        token = streaming_conversation.set(conversation_id)  # invariant 3
        collector_token = subagent_events.set([])
        # Record this turn's output for a user who switches into its conversation before it finishes;
        # ended below by `_persist` (and again in the `finally`, for a turn that never got that far).
        self._ui.begin_catch_up(conversation_id, msg.text, msg.images)
        succeeded = False
        failure_reason = ""  # set on error, so a backgrounded turn's notification can carry the reason
        # Where this turn's sub-agent cards anchor. Both branches settle it in a `finally`, so the
        # cancellation and error paths below record under the index a completed turn would use; -1
        # means the turn committed no user message, and recording no-ops.
        user_index = -1
        try:
            async with self._gate.turn(conversation_id):  # invariant 1
                logger.info("turn %s gate entered (%s)", tid, conversation_id)
                try:
                    if plan:
                        runner = PlanRunner(agent, self._ui, self._config, self._review_plan)
                        try:
                            result = await runner.run(msg)
                        finally:
                            # A cancelled or failed planned turn raises instead of returning a
                            # PlanResult, and the runner publishes the index as it commits, so read it
                            # from the runner rather than from a result that may never arrive.
                            user_index = runner.user_index
                        self._book.record_plan_metadata(result, conversation_id)
                    else:
                        # Taken here rather than before the gate: it is the lower bound the turn's own
                        # user message is searched for from, so anything another turn appended first
                        # has to be behind it.
                        base_len = len(agent.model_client.messages)
                        try:
                            stream = await agent.run(msg.text, stream=True, images=msg.images)
                            await self._ui.send(stream, reply_to=msg)
                        finally:
                            # Resolved after the run, not taken as base_len itself: the user message
                            # this anchors to does not exist until the run appends it, and a first turn
                            # seeds the system message ahead of it. Reached on the cancelled path too,
                            # where the agent has already snapshotted the partial turn in its finally.
                            user_index = resolve_user_index(agent.model_client.messages, base_len)
                except asyncio.CancelledError:
                    # `/stop` (or shutdown) cancelled this turn. Record first: the "(stopped)" send
                    # below is one more await, and a second cancellation racing it would otherwise
                    # propagate straight past a record placed after -- see invariant 5. Keep the
                    # partial state (the agent snapshots it in a finally), and return so the daemon
                    # keeps serving.
                    self._record_subagents(conversation_id, user_index)
                    logger.info("turn %s cancelled after %.1fs", tid, time.monotonic() - started)
                    try:
                        await self._ui.send("(stopped)", reply_to=msg)
                    except Exception:
                        pass
                    await self._persist(conversation_id)
                    return
                except ModelConnectionError as exc:
                    self._record_subagents(conversation_id, user_index)  # before the send: invariant 5
                    logger.exception("turn %s connection error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"couldn't reach the model server: {describe_error(exc)}"
                    await self._ui.send(
                        f"The request couldn't reach the model server: {describe_error(exc)}", reply_to=msg
                    )
                except Exception as exc:
                    self._record_subagents(conversation_id, user_index)  # before the send: invariant 5
                    logger.exception("turn %s error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"failed: {describe_error(exc)}"
                    await self._ui.send(f"Sorry, the request failed: {describe_error(exc)}", reply_to=msg)
                else:
                    self._record_subagents(conversation_id, user_index)
                    logger.info("turn %s done after %.1fs", tid, time.monotonic() - started)
                    succeeded = True
                await self._persist(conversation_id)
        finally:
            subagent_events.reset(collector_token)
            streaming_conversation.reset(token)
            # Normally already done by `_persist`; this covers a turn that raised before reaching it,
            # whose record would otherwise linger and replay a phantom user bubble on the next switch-in.
            self._ui.end_catch_up(conversation_id)
            self._book.unpin(conversation_id)
        await self._notify_if_backgrounded(conversation_id, succeeded=succeeded, failure_reason=failure_reason)

    def _record_subagents(self, conversation_id: str, user_index: int) -> None:
        """Persist whatever the turn's spawns reported. Synchronous, so it also runs on the cancelled
        path where an await could be cut short."""
        self._book.record_subagent_events(subagent_events.get() or [], user_index, conversation_id)

    async def _notify_if_backgrounded(self, conversation_id: str, *, succeeded: bool, failure_reason: str) -> None:
        """The user switched away before this turn finished: tell them rather than silently updating
        a conversation they are not looking at.

        The reply (or the error message) went out muted, so this notification is the only signal they
        get. A cancelled turn returns before this point, so it never notifies. On failure the reason
        rides along, because the muted error message is not persisted and so is not visible on
        switching back in.
        """
        if conversation_id == self._book.active_id:
            return
        title = self._book.get(conversation_id).metadata.get("title") or "a conversation"
        if succeeded:
            await self._ui.notify(f"Reply ready in '{title}'.")
        else:
            await self._ui.notify(f"A reply in '{title}' {failure_reason}.")

    # --- proactive ------------------------------------------------------------------------------

    async def proactive(
        self,
        prompt: str,
        *,
        target: str = "active",
        task_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Run an unattended turn with ``prompt`` and surface the result.

        The substrate for scheduled tasks: the scheduler fires this with the task's instruction.
        ``target`` selects the conversation (see :class:`ProactiveTarget`):

        - ``"active"``: the conversation currently being viewed.
        - ``"new"``: a fresh conversation minted for this firing alone.
        - ``"task"``: the task's own conversation, reused across firings -- ``session_id`` is the key
          it created previously (``None`` on the first firing).

        Returns the conversation key for ``"new"``/``"task"`` so the caller can remember it, or
        ``None`` for ``"active"``. **The key is returned even when the run fails**, so a task whose
        first firing errors still records the conversation it minted instead of minting another one
        on every subsequent firing.
        """
        spec = self._resolve_target(target, prompt, task_name, session_id)
        try:
            await self._run_unattended(prompt, spec)
        except ModelConnectionError as exc:  # invariant 6
            logger.exception("proactive turn connection error")
            await self._report(f"A scheduled task couldn't reach the model server: {describe_error(exc)}")
        except Exception as exc:  # invariant 6
            logger.exception("proactive turn error")
            await self._report(f"A scheduled task failed: {describe_error(exc)}")
        else:
            if spec.announce:
                await self._push_conversations()
                await self._report(spec.announce)
        return spec.conversation_id if spec.returns_key else None

    def _resolve_target(
        self, target: str, prompt: str, task_name: Optional[str], session_id: Optional[str]
    ) -> ProactiveTarget:
        """Pick the conversation this firing runs in, minting or reusing one as the target requires.

        A ``"new"``/``"task"`` target degrades to ``"active"`` on a channel with no conversation list:
        the user would have no way to reach a conversation they cannot see.
        """
        if target not in ("new", "task") or not self._ui.supports_conversations:
            return ProactiveTarget(conversation_id=self._book.active_id, echo_reply=True)

        reuse = session_id if target == "task" else None  # "new" always mints a fresh conversation
        if reuse and self._book.exists(reuse):
            session = self._book.get(reuse)
            self._book.touch(session)
        else:
            title = task_name or derive_title([{"role": "user", "content": prompt}]) or "Scheduled task"
            session = self._book.new_session(title=title)
        title = session.metadata.get("title") or "Scheduled task"
        return ProactiveTarget(
            conversation_id=session.key,
            echo_reply=False,
            announce=f"Scheduled task '{title}' finished; open the '{title}' conversation to review.",
            returns_key=True,
        )

    async def _run_unattended(self, prompt: str, spec: ProactiveTarget) -> None:
        """The one body every proactive target shares. See the module's concurrency invariants."""
        conversation_id = spec.conversation_id
        token = streaming_conversation.set(conversation_id)  # invariant 3
        collector_token = subagent_events.set([])
        proactive_token = proactive_turn.set(True)  # gated tools auto-deny for the whole run
        self._book.pin(conversation_id)  # invariant 2
        try:
            async with self._gate.turn(conversation_id):  # invariant 1
                # Record the turn for a user who switches into its conversation before it finishes. An
                # unattended turn's only display frames are its spawns' cards, so without this a task
                # delegating in a conversation nobody is watching shows nothing of that work on the
                # switch-in, and the spawn's later `append` frames arrive with no card to update.
                # Opened inside the gate rather than beside the contextvars above (as the reactive path
                # does; see invariant 3): a firing that has to queue behind a turn already running on
                # this conversation would otherwise replace that turn's record while it is still needed.
                self._ui.begin_catch_up(conversation_id, prompt)
                agent = self._book.agent_for(conversation_id)
                # Tag every message this unprompted run appends, so replayed history can distinguish
                # it from a user-driven turn. The agent doesn't reset on run (the system prompt lives
                # on the client), so the pre-run length is a stable start index for the exchange.
                start = len(agent.model_client.messages)
                reply = await agent.run(prompt)
                for message in agent.model_client.messages[start:]:
                    message[PROVENANCE_KEY] = PROVENANCE_PROACTIVE
                if spec.echo_reply:
                    await self._ui.send(reply)
                self._record_subagents(conversation_id, resolve_user_index(agent.model_client.messages, start))
                await self._persist(conversation_id)
        finally:
            self._book.unpin(conversation_id)
            # Normally already done by `_persist`; this covers a run that raised before reaching it.
            self._ui.end_catch_up(conversation_id)
            proactive_turn.reset(proactive_token)
            subagent_events.reset(collector_token)
            streaming_conversation.reset(token)

    async def _report(self, text: str) -> None:
        """Send an unattended run's own status line, tolerating a channel that cannot take it.

        Nobody is awaiting this turn, so a failed notification must not become the error that takes
        down the scheduler job (invariant 6).
        """
        try:
            await self._ui.send(text)
        except Exception:
            logger.warning("A scheduled task ran; its notification could not be delivered", exc_info=True)

    async def _persist(self, conversation_id: str) -> None:
        """Snapshot the turn onto its session, refreshing the sidebar if a title was just derived."""
        title_derived = self._book.persist(conversation_id)
        # The store now holds what the catch-up record stood in for. Dropped here, between the write and
        # the next await, rather than in the caller's `finally`: a switch-in landing in between would
        # otherwise replay the turn twice, once from history and once from the record.
        self._ui.end_catch_up(conversation_id)
        if title_derived:
            await self._push_conversations()
