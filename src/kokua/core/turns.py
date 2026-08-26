"""Running a turn: reactive (the user sent something) and proactive (a scheduled task fired).

## Concurrency invariants

Every rule here was learned from a bug. Read them before changing anything in this module.

1. **One gate hold per task, taken exactly once.** ``TurnGate`` is writer-preferring: a waiting
   exclusive() blocks new readers. A task that holds a reader and then tries to take a *second*
   reader deadlocks against a concurrent exclusive(), which is waiting for the reader count to reach
   zero that the outer hold will never release. Every path below takes exactly one
   ``gate.turn(...)``, and no path calls another path that takes one. Do not wrap a call to
   ``reactive`` or ``proactive`` in a gate hold. An unattended run's hold is taken by
   ``_run_unattended`` and the child task it starts takes none of its own, which keeps the count at one
   for the firing as a whole; the gate's per-conversation lock is an ``asyncio.Lock``, which has no
   owning task, so acquiring and releasing it around a child is sound.
   The rule reaches one holder outside this module: ``ConversationBook.delete`` takes the deleted
   conversation's own ``gate.turn`` (it used to take the writer, which made a delete wait out every
   unrelated conversation's turn and froze the web front end's socket reader). So a path here that
   deletes has to be outside its own hold, which is why ``_prune_task_conversations`` runs after
   ``_run_unattended`` returns rather than inside it.
   (Regressions: ``test_proactive_new_session_holds_at_most_one_gate_turn``,
   ``test_delete_does_not_wait_for_a_turn_on_another_conversation``.)

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
   (or into no list at all) instead of the turn that owns it. ``current_metrics`` (``core/metrics.py``)
   follows the identical discipline for the same reason: set before the first ``await``, reset only in
   the ``finally``, so a model call racing the window is never attributed to a finished turn's record or
   to no turn at all. An unattended run cannot share the reactive path's ``finally`` (it runs in a child
   task with no such block of its own), so ``_unattended_body`` opens and resets its own
   ``current_metrics`` scope around its whole body, the same way it opens its own catch-up record rather
   than reusing the caller's. The channel's catch-up record is opened
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
   ``_record_provenance`` is synchronous, but that only protects it from a cancellation that arrives
   *after* it runs. A cancelled or failed turn still does one more ``await`` of its own (the
   "(stopped)" notice, or the failure message) before falling through to the shared ``_persist``
   call; a second cancellation delivered during that send raises ``CancelledError`` again, which
   propagates past a record call placed after the send and drops the turn's events. Every branch --
   cancelled, connection error, generic error, success -- records as its first action, not as a step
   shared only by the paths that return normally. Recording under the right index is half of this,
   and every path resolves that index with ``resolve_user_index`` (the pre-run length is not it: a
   first turn seeds the system message ahead of the user message). A workflow turn's index cannot come
   from its ``WorkflowResult`` either, since a cancelled or failed run raises instead of returning one,
   so a workflow publishes the index through ``WorkflowContext.publish_user_index`` as it commits and
   the workflow branch reads ``ctx.user_index`` back in a ``finally``. The rule binds the unattended
   path too, which is why ``_run_unattended`` holds a failed run's error rather than letting it
   propagate: the record and the snapshot have to happen before ``proactive`` reports the failure, and
   an error on its way out of the gate hold would carry the turn straight past both.
   (Regressions: ``test_a_second_cancellation_during_the_stopped_send_still_records``,
   ``test_a_cancelled_rich_workflow_still_records_its_published_events``,
   ``test_a_failed_rich_workflow_still_records_its_published_events``,
   ``test_a_first_turns_cards_anchor_to_its_user_message_past_the_system_message``.)

6. **An unattended turn never lets an exception escape.** A scheduled firing has no user awaiting it
   and runs inside a scheduler job with no handler of its own, so a propagating error would take the
   scheduler down with it. Report it on the channel and swallow it. Invariant 7 is the same rule for
   the one cancellation that is not an error.

7. **A stop ends a firing; a shutdown ends the process. Both arrive as a cancellation.** An unattended
   run is stoppable, which means something has to be cancellable, and the two candidates are not
   interchangeable. Cancelling the task the firing runs *in* is the scheduler's job: that is what
   ``Scheduler.cancel`` reaches, and it unregisters the job as well, so stopping a run that way would
   silently disarm the task's schedule. So ``_run_unattended`` runs the turn in a child task, registers
   *that* in the ``TurnTracker`` (keyed by conversation, like a reactive turn's, and carrying the task id
   a stop looks it up by), and a stop cancels the child. The firing then ends and its scheduler job
   returns to re-arm as if the run had finished.
   Telling the two apart is the subtle half. Cancelling a task cascades into the task it is awaiting, so
   the child is cancelled either way and its own state says nothing; ``asyncio.current_task().cancelling()``
   is the discriminator, since it counts only the cancellations aimed at this task. A stop is converted
   into an ordinary return (invariant 6's shape), while a cancellation aimed here keeps propagating,
   taking the child with it. The child records and persists its partial turn before ending either way,
   for invariant 5's reason.
   The tracker entry is added *inside* the gate, alongside the catch-up record and for the same reason:
   a firing queued behind a turn already running on this conversation would otherwise overwrite that
   turn's entry, which is the one ``/stop`` reaches. A queued firing is therefore not yet stoppable. The
   converse costs the same and only on a channel with no conversation list, where a firing shares the
   viewed conversation: the serve loop tracks a reactive turn when it is *submitted* rather than when it
   takes the gate, so a message sent during a firing replaces the firing's entry, and the stop that
   message queued behind is the one a stop then reaches. Both follow from one entry per conversation,
   which is what keeps a finished turn from ever cancelling a live one.

   Shutdown is the one reader that must not follow that rule, because it closes the session store.
   Replacing an entry does not end the turn it replaced, so the per-conversation entries are not the
   list of turns still running; ``TurnTracker.live()`` is, and shutdown cancels and awaits that instead.
   A turn left out of it is cancelled by the event loop after the store has closed, and the record
   invariant 5 makes on the way down then raises ``I/O operation on closed file`` out of a task nobody
   is watching, losing that turn's partial answer with it.
   (Regressions: ``test_a_running_firing_is_tracked_under_its_task_so_it_can_be_stopped``,
   ``test_shutdown_cancellation_still_takes_the_firing_down_with_it``,
   ``test_shutdown_waits_for_a_turn_a_later_message_displaced``.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, Union

from aimu import PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.aio import ModelConnectionError, RunHandle
from aimu.aio.channels.base import ChannelMessage
from aimu.sessions import Session

from kokua.channels.web import proactive_turn, streaming_conversation
from kokua.config.file import thinking_request
from kokua.core.build import model_label
from kokua.core.errors import describe_error
from kokua.core.messages import derive_title, resolve_user_index
from kokua.core.metrics import TurnMetrics, current_metrics, record_event
from kokua.core.subagents import subagent_events
from kokua.core.turn_registry import TurnInfo
from kokua.workflows import SettingsView, WorkflowContext, is_rich

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProactiveTarget:
    """Which conversation an unattended run uses, and how it reports itself.

    A firing normally mints its own conversation, which the user is by definition not looking at, so
    instead of echoing a reply into nowhere it announces that the run finished. On a channel with no
    conversation list there is nowhere else to put it, so it runs in the conversation being viewed and
    echoes the reply there.

    ``prunes_for_task`` is the task whose retention cap this run should enforce afterwards, set only on
    the minting path: a firing that fell back to the viewed conversation minted nothing, and that
    conversation belongs to the user rather than to the task.

    ``task_id`` is which task is firing, and unlike ``prunes_for_task`` it is set on both paths: it is
    what a stop looks the run up by, and a firing is no less stoppable for having run in the conversation
    the user was already looking at.
    """

    conversation_id: str
    echo_reply: bool
    announce: Optional[str] = None
    prunes_for_task: Optional[str] = None
    task_id: Optional[str] = None


def _holds_no_report(session: Session) -> bool:
    """Whether a task's conversation holds nothing the user would keep over another run's output.

    The retention order in :meth:`TurnRunner._prune_task_conversations` reads this: a run that has no
    report to show is what a cap evicts first, so a task that fails repeatedly cannot cost the user
    the last firing that actually produced something.

    Two ways to hold nothing, because a recorded failure only covers one of them. The reason is keyed
    to the turn's user message, so a firing that raised before its user turn reached the transcript --
    an agent that would not build, a client that failed to construct -- has no turn to key one to. An
    empty transcript says the same thing on its own.
    """
    return bool(session.metadata.get("failure")) or not session.messages


class TurnRunner:
    def __init__(self, book, ui, gate, config, *, tracker, decide, push_conversations, delete_conversation, state=None):
        self._book = book
        # Where a firing registers itself while it runs, so a stop can reach it. The reactive path's own
        # entries are added by the serve loop, which owns the handle it starts.
        self._tracker = tracker
        self._ui = ui
        self._gate = gate
        self._config = config
        # Handed to a workflow through its context, so the core never learns any workflow's reply
        # vocabulary.
        self._decide = decide
        self._push_conversations = push_conversations
        # The assistant's own delete, not the book's: it also abandons a pending approval and switches
        # the view away, which matters when a firing prunes the run the user is reading.
        self._delete_conversation = delete_conversation
        # Shared toolset state, passed through to a workflow's context. Assigned after construction by
        # the composition root, which builds it later (see Assistant.create).
        self.state = state

    # --- reactive -------------------------------------------------------------------------------

    async def reactive(
        self, msg: ChannelMessage, *, conversation_id: str, workflow=None, tid: Optional[int] = None
    ) -> None:
        """Run a user-initiated turn and send its reply. See the module's concurrency invariants."""
        started = time.monotonic()
        agent = self._book.agent_for(conversation_id)
        # The effort this turn runs at, resolved once so the run and the record cannot disagree. A
        # per-turn request rides the message (the web composer's picker, the CLI's /think) and applies to
        # the entry agent's own run, which is the plain-turn branch below. A workflow drives its agents at
        # the efforts their tables declare, so a request arriving on a workflow turn applies to nothing,
        # and recording it would make the transcript claim an effort the turn never ran at.
        declared = self._config.thinking_for(self._config.entry_agent)
        # `.metadata or {}`, not `.metadata`: `ChannelMessage.metadata` defaults to `{}` and no channel in
        # this repo or in AIMU ever sets it to `None`, but this is the one seam a message from an unknown
        # channel arrives on, so it is where that belief gets guarded rather than assumed.
        requested = thinking_request((msg.metadata or {}).get("thinking")) if workflow is None else None
        thinking = declared if requested is None else requested
        self._book.pin(conversation_id)  # invariant 2
        token = streaming_conversation.set(conversation_id)  # invariant 3
        collector_token = subagent_events.set([])
        metrics = TurnMetrics()
        metrics_token = current_metrics.set(metrics)
        # The client carries the forwarder, not this turn's accumulator: the forwarder holds no turn
        # state, so it is safe as the durable client-wide setting AIMU calls it, and the contextvar
        # above is what keeps concurrent turns on other conversations out of this record. Assigned
        # here rather than at agent build time only because a test may inject its own client.
        agent.model_client.events = record_event
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
                    if workflow is not None:
                        ctx = self._workflow_context(agent, msg, workflow)
                        runner = workflow.build(ctx)
                        try:
                            if is_rich(runner):
                                result = await runner.run_turn()
                                self._book.record_workflow_metadata(result, conversation_id)
                            else:
                                await self._drive_base_tier(runner, msg, ctx)
                        finally:
                            # A cancelled or failed workflow raises instead of returning a result, and
                            # a rich one publishes its index as it commits, so read it from the context
                            # rather than from a result that may never arrive.
                            user_index = ctx.user_index
                    else:
                        # Taken here rather than before the gate: it is the lower bound the turn's own
                        # user message is searched for from, so anything another turn appended first
                        # has to be behind it.
                        base_len = len(agent.model_client.messages)
                        try:
                            stream = await agent.run(msg.text, stream=True, images=msg.images, thinking=thinking)
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
                    self._record_provenance(
                        conversation_id, user_index, thinking=thinking, metrics=metrics, started=started
                    )
                    logger.info("turn %s cancelled after %.1fs", tid, time.monotonic() - started)
                    try:
                        await self._ui.send("(stopped)", reply_to=msg)
                    except Exception:
                        pass
                    await self._persist(conversation_id)
                    return
                except ModelConnectionError as exc:
                    # before the send: invariant 5
                    self._record_provenance(
                        conversation_id, user_index, thinking=thinking, metrics=metrics, started=started
                    )
                    logger.exception("turn %s connection error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"couldn't reach the model server: {describe_error(exc)}"
                    await self._ui.send(
                        f"The request couldn't reach the model server: {describe_error(exc)}", reply_to=msg
                    )
                except Exception as exc:
                    # before the send: invariant 5
                    self._record_provenance(
                        conversation_id, user_index, thinking=thinking, metrics=metrics, started=started
                    )
                    logger.exception("turn %s error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"failed: {describe_error(exc)}"
                    await self._ui.send(f"Sorry, the request failed: {describe_error(exc)}", reply_to=msg)
                else:
                    self._record_provenance(
                        conversation_id, user_index, thinking=thinking, metrics=metrics, started=started
                    )
                    logger.info("turn %s done after %.1fs", tid, time.monotonic() - started)
                    succeeded = True
                await self._persist(conversation_id)
        finally:
            current_metrics.reset(metrics_token)
            subagent_events.reset(collector_token)
            streaming_conversation.reset(token)
            # Normally already done by `_persist`; this covers a turn that raised before reaching it,
            # whose record would otherwise linger and replay a phantom user bubble on the next switch-in.
            self._ui.end_catch_up(conversation_id)
            self._book.unpin(conversation_id)
        await self._notify_if_backgrounded(conversation_id, succeeded=succeeded, failure_reason=failure_reason)

    def _workflow_context(self, agent, msg: ChannelMessage, workflow) -> WorkflowContext:
        """One turn's context for ``workflow``.

        ``commit_user_message`` is bound here rather than implemented by the workflow because finding
        the committed user message is the core's own subtlety: a first turn seeds the system message
        ahead of it, so its position cannot be assumed from a pre-run length (see ``resolve_user_index``).
        """

        def commit_user_message(base_len: int, text: str) -> None:
            index = resolve_user_index(agent.model_client.messages, base_len)
            ctx.publish_user_index(index)
            if index >= 0:
                agent.model_client.messages[index]["content"] = text

        ctx = WorkflowContext(
            agent=agent,
            ui=self._ui,
            config=self._config,
            # The carrying toolset's own config section, which is why this is keyed by the workflow's
            # name: build_command_map refuses a workflow whose name differs from its toolset's.
            settings=SettingsView(self._config.toolset_settings.get(workflow.name, {})),
            msg=msg,
            state=self.state,
            decide=self._decide,
            commit_user_message=commit_user_message,
        )
        return ctx

    async def _drive_base_tier(self, runner, msg: ChannelMessage, ctx: WorkflowContext) -> None:
        """Stream a plain ``AsyncRunner`` into the reply. Not persisted.

        A *self-contained* runner (one that never touches ``ctx.agent``) appends nothing to the agent's
        own transcript, so ``resolve_user_index`` finds no new user message here and publishes -1:
        nothing of this exchange reaches ``_persist``'s snapshot or the sub-agent record. The reply
        reaches the channel and nothing else -- reloading the conversation will not show it. A runner
        that closes over ``ctx.agent`` and runs it directly does append, and persists normally. Whether
        a self-contained base-tier turn's own exchange should be persisted is a product question this
        plan leaves open.
        """
        base_len = len(ctx.agent.model_client.messages)
        try:
            stream = await runner.run(msg.text, stream=True, images=msg.images)
            await self._ui.send(stream, reply_to=msg)
        finally:
            ctx.publish_user_index(resolve_user_index(ctx.agent.model_client.messages, base_len))

    def _record_provenance(
        self,
        conversation_id: str,
        user_index: int,
        failure: Optional[str] = None,
        *,
        thinking: Optional[Union[bool, str]],
        metrics: Optional[TurnMetrics],
        started: Optional[float],
    ) -> None:
        """Persist what produced this turn: whatever its spawns reported, the model that answered, the
        reasoning effort it ran at, why it stopped early if it did, and what it cost. Synchronous, so it
        also runs on the cancelled path where an await could be cut short.

        ``thinking`` is passed in rather than read from the config here, and is keyword-only and required
        so no caller can forget it. A turn can now carry its own effort request, so the config says what a
        turn *would* have run at and this record has to say what it *did*. Each caller resolves the value
        once and hands the same one to the run and to this record, which is what keeps the two in step.

        ``metrics`` and ``started`` are the turn's sink and its start, not a finished record, so that the
        wall-clock figure is measured at the moment of recording. That matters on the failure paths: a
        turn that raised still cost what it cost up to the point it stopped, and a record made from a
        duration computed earlier would under-report exactly the turns a reader most wants to examine.
        Keyword-only and required, with no default, for the same reason ``thinking`` has none: a sixth
        call site that forgot them would silently record a turn as having cost nothing, which is the
        one failure mode worth making impossible to omit by accident.
        """
        usage = None
        if metrics is not None and started is not None:
            usage = metrics.record(wall_seconds=time.monotonic() - started)
        self._book.record_turn_provenance(
            subagent_events.get() or [],
            self._answering_model(conversation_id),
            user_index,
            conversation_id,
            thinking=thinking,
            failure=failure,
            usage=usage,
        )

    def _answering_model(self, conversation_id: str) -> str:
        """The model behind this conversation's agent, as a string for the stored record.

        Takes ``conversation_id`` for the caller's benefit rather than its own: every conversation's
        agent IS the entry agent (only a spawned worker differs), so the answer is the same for all of
        them, and keeping the argument means a caller does not have to know that to ask the question.
        """
        return model_label(self._config, self._config.entry_agent)

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
        task_name: Optional[str] = None,
        task_id: Optional[str] = None,
        max_conversations: int = 0,
    ) -> None:
        """Run an unattended turn with ``prompt`` and surface the result.

        The substrate for scheduled tasks: the scheduler fires this with the task's instruction. The
        firing runs in a conversation minted for it and stamped with ``task_id``, so a front end can
        group a task's runs under it and retention knows which conversations the task owns. A channel
        with no conversation list has nowhere to put such a conversation, so there the run falls back
        to the one being viewed and stamps nothing: that conversation belongs to the user.

        ``max_conversations`` is how many of this task's conversations survive the firing, ``0``
        meaning unlimited. Pruning happens whether or not the run succeeded, and whether or not it was
        stopped (see :meth:`_prune_task_conversations`).

        A run can be stopped while it is in flight; invariant 7 covers how. Here that is just a third way
        to end: the announce is withheld, since there is no output to send the user to, and everything
        else -- the prune, the refreshed list -- happens as it does for a run that finished.
        """
        spec = self._resolve_target(prompt, task_name, task_id)
        report: Optional[str] = None
        try:
            stopped = await self._run_unattended(prompt, spec)
        except ModelConnectionError as exc:  # invariant 6
            logger.exception("proactive turn connection error")
            report = f"A scheduled task couldn't reach the model server: {describe_error(exc)}"
        except Exception as exc:  # invariant 6
            logger.exception("proactive turn error")
            report = f"A scheduled task failed: {describe_error(exc)}"
        else:
            # A stopped run has no output to send anyone to, so it says nothing rather than announcing
            # itself as finished. The user asked for the stop; the run's own conversation records it.
            report = None if stopped else spec.announce
        await self._prune_task_conversations(spec, max_conversations)
        # Every path refreshes the list, because every path has to clear the running marker the run put
        # on its conversation when it started.
        await self._push_conversations()
        if report:
            await self._report(report)

    async def _prune_task_conversations(self, spec: ProactiveTarget, cap: int) -> None:
        """Keep the firing task's newest ``cap`` conversations and delete the rest, once this run is done.

        Runs on every path, not only where the firing succeeded: a task that fails on every firing was
        otherwise never pruned at all, and minted an unbounded pile of conversations the cap was there
        to cover. Always *after* ``_run_unattended`` has returned, though, because the delete takes a
        ``gate.turn`` of its own and this firing was holding one (invariant 1). The firing's own
        conversation is a prune candidate here, so run inside the hold this would not merely add a second
        reader, it would wait on the per-conversation lock the same task already owns.

        Eviction order is failed runs before successful ones, then oldest before newest, and the
        conversation this firing just used is a candidate like any other. That ordering is what lets the
        failure path prune safely: at a cap of 1, evicting strictly oldest-first would drop the last good
        report in favour of the failure that followed it, so instead the failure is what goes. On the
        success path the same ordering leaves this firing's own conversation alone (it is the newest, and
        it did not fail), so a cap of 1 still replaces the previous run rather than the one just written.

        Failures are swallowed the way the run's own are (invariant 6): the user may have deleted a
        run themselves between firings, and a task must not stop firing over a conversation nobody has.
        """
        if not spec.prunes_for_task or cap <= 0:
            return
        owned = self._book.sessions_for_task(spec.prunes_for_task)  # oldest first
        owned.sort(key=_holds_no_report, reverse=True)  # stable, so oldest-first survives within each group
        for session in owned[: max(0, len(owned) - cap)]:
            try:
                await self._delete_conversation(session.key)
            except Exception:
                logger.warning("Could not prune a conversation a scheduled task replaced", exc_info=True)

    def _resolve_target(
        self,
        prompt: str,
        task_name: Optional[str],
        task_id: Optional[str] = None,
    ) -> ProactiveTarget:
        """Mint the conversation this firing runs in, or fall back to the viewed one.

        The fallback is for a channel with no conversation list: the user would have no way to reach a
        conversation they cannot see.
        """
        if not self._ui.supports_conversations:
            return ProactiveTarget(conversation_id=self._book.active_id, echo_reply=True, task_id=task_id)

        title = task_name or derive_title([{"role": "user", "content": prompt}]) or "Scheduled task"
        session = self._book.new_session(title=title, task_id=task_id)
        title = session.metadata.get("title") or "Scheduled task"
        return ProactiveTarget(
            conversation_id=session.key,
            echo_reply=False,
            announce=f"Scheduled task '{title}' finished; open the '{title}' conversation to review.",
            prunes_for_task=task_id,
            task_id=task_id,
        )

    async def _run_unattended(self, prompt: str, spec: ProactiveTarget) -> bool:
        """Run one unattended turn, returning whether it was stopped instead of allowed to finish.

        The body runs in a child task, which is what a stop cancels. Stopping the child rather than this
        task is what keeps the schedule intact: this task is the scheduler's job, and ``Scheduler.cancel``
        would reach it but unregisters the job too, silently disarming the task. It also leaves the job
        free to re-arm afterwards, since a stopped firing returns to it normally.
        """
        conversation_id = spec.conversation_id
        token = streaming_conversation.set(conversation_id)  # invariant 3
        collector_token = subagent_events.set([])
        proactive_token = proactive_turn.set(True)  # gated tools auto-deny for the whole run
        self._book.pin(conversation_id)  # invariant 2
        try:
            # Held here rather than in the child so there is exactly one hold for the firing either way
            # (invariant 1). An asyncio lock has no owning task, so releasing it here is sound.
            async with self._gate.turn(conversation_id):  # invariant 1
                handle = RunHandle.start(self._unattended_body(prompt, spec))
                # Tracked inside the gate, for the same reason the catch-up record is opened there: a
                # firing queued behind a turn already running on this conversation would otherwise
                # overwrite that turn's entry, which is the one `/stop` and shutdown reach. A queued
                # firing is therefore not yet stoppable, which is what the tracker's one-entry-per-
                # conversation rule buys. Registered before the first await below, so the entry is in
                # place by the time the child's first statement runs.
                self._tracker.add(
                    conversation_id,
                    TurnInfo(handle=handle, started=time.monotonic(), preview=prompt[:120], task_id=spec.task_id),
                )
                try:
                    await handle.result()
                except asyncio.CancelledError:
                    # Two different cancellations land here and have to end differently: a stop, which
                    # cancels the child and is this firing's own ending, and a shutdown, which cancels
                    # *this* task and has to keep propagating. The child's state cannot tell them apart
                    # (cancelling a task cascades into the task it is awaiting, so the child is cancelled
                    # either way); `cancelling()` can, since it counts only the cancellations aimed here.
                    if asyncio.current_task().cancelling():
                        handle.cancel()  # already cascaded, except if we were cancelled outside the await
                        raise
                    logger.info("scheduled firing in %s was stopped", conversation_id)
                    return True
                finally:
                    self._tracker.remove_if(conversation_id, handle)
        finally:
            self._book.unpin(conversation_id)
            # Normally already done by `_persist`; this covers a run that raised before reaching it.
            self._ui.end_catch_up(conversation_id)
            proactive_turn.reset(proactive_token)
            subagent_events.reset(collector_token)
            streaming_conversation.reset(token)
        return False

    async def _unattended_body(self, prompt: str, spec: ProactiveTarget) -> None:
        """One unattended turn, inside its caller's gate hold. See the module's concurrency invariants.

        Ends cancelled when it was stopped, as a cancelled task should, having first recorded and
        persisted as much of the turn as it got: ``_run_unattended`` is what turns that back into an
        ordinary return.
        """
        conversation_id = spec.conversation_id
        started = time.monotonic()
        # This run's own accumulator and sink, opened and torn down here rather than by the caller: an
        # unattended turn has no counterpart to the reactive path's outer `finally`, since the child task
        # this runs in ends by returning or raising, not by falling through a shared teardown block.
        metrics = TurnMetrics()
        metrics_token = current_metrics.set(metrics)
        try:
            # The run's conversation reaches the sidebar here, at the start, marked as running: it is what
            # the task panel offers a Stop button against, and a firing that only pushed on success left a
            # run in flight (or one that failed) invisible in the list.
            await self._push_conversations()
            # Record the turn for a user who switches into its conversation before it finishes. An
            # unattended turn's only display frames are its spawns' cards, so without this a task
            # delegating in a conversation nobody is watching shows nothing of that work on the
            # switch-in, and the spawn's later `append` frames arrive with no card to update.
            # Opened inside the gate rather than beside the contextvars in the caller (as the reactive
            # path does; see invariant 3): a firing that has to queue behind a turn already running on
            # this conversation would otherwise replace that turn's record while it is still needed.
            self._ui.begin_catch_up(conversation_id, prompt)
            agent = self._book.agent_for(conversation_id)
            # See the identical assignment (and its comment) in `reactive`: the forwarder is the durable,
            # client-wide setting, and the contextvar above is what keeps this run's record isolated.
            agent.model_client.events = record_event
            # The agent doesn't reset on run (the system prompt lives on the client), so the
            # pre-run length is a stable start index for the exchange.
            start = len(agent.model_client.messages)
            # A failed run is snapshotted as far as it got, so the conversation the firing minted is
            # never indistinguishable from one that never ran: the model client appends the user turn
            # before it sends the request, so even a firing that got no answer has its prompt (and
            # any completed tool rounds) on the transcript. The error is held rather than allowed to
            # propagate straight out, so the record and the snapshot below run on every path, and
            # re-raised afterwards so `proactive` still logs the traceback and tells the user.
            # Deliberately not a `finally`: `_persist` awaits, and a store failure raised from a
            # `finally` would replace the run's own error as the reason reported.
            error: Optional[BaseException] = None
            failure: Optional[str] = None
            stopped = False
            try:
                reply = await agent.run(prompt)
                if spec.echo_reply:
                    await self._ui.send(reply)
            except asyncio.CancelledError:
                # Stopped. Handled alongside the failures rather than left to propagate, so the partial
                # turn is recorded and snapshotted the way a failed one is; re-raised below, after that.
                stopped, failure = True, "stopped"
            except ModelConnectionError as exc:
                error, failure = exc, f"couldn't reach the model server: {describe_error(exc)}"
            except Exception as exc:
                error, failure = exc, f"failed: {describe_error(exc)}"
            for message in agent.model_client.messages[start:]:
                # Tag every message this unprompted run appended, so replayed history can distinguish
                # it from a user-driven turn. setdefault, not assignment: the agent loop tags the
                # turns it injects itself (`continuation`, `final_answer`), and those tags are how a
                # transcript tells an injected nudge from something the user typed. Overwriting them
                # made every nudge replay as a user bubble.
                message.setdefault(PROVENANCE_KEY, PROVENANCE_PROACTIVE)
            # The reason is recorded here rather than left to `_report`, whose status line goes to
            # whichever conversation the user is viewing rather than to this one. Before the persist,
            # and synchronously, for invariant 5's reason.
            self._record_provenance(
                conversation_id,
                resolve_user_index(agent.model_client.messages, start),
                failure=failure,
                # An unattended run has no message from a user and so carries no request: the configured
                # effort is both what it would run at and what it did.
                thinking=self._config.thinking_for(self._config.entry_agent),
                metrics=metrics,
                started=started,
            )
            await self._persist(conversation_id)
            if stopped:
                if spec.echo_reply:  # the user is watching this conversation, as the CLI's one user is
                    try:
                        await self._ui.send("(stopped)")
                    except Exception:
                        pass
                raise asyncio.CancelledError
            if error is not None:
                raise error
        finally:
            current_metrics.reset(metrics_token)

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
