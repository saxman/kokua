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
   The rule reaches three holders outside this module, all in ``ConversationBook``: ``delete``,
   ``retitle``, and ``truncate``, each taking that one conversation's own ``gate.turn`` (``delete`` used
   to take the writer, which made a delete wait out every unrelated conversation's turn and froze the web
   front end's socket reader). So a path here that deletes has to be outside its own hold, which is why
   ``_prune_task_conversations`` runs after ``_run_unattended`` returns rather than inside it.
   ``truncate`` adds a fact the other two do not have: it *drops this conversation's cached agent* while
   a turn may still hold a reference to it, since ``reactive`` fetches its agent before taking the gate,
   so a turn queued behind a truncation finishes on an agent the registry has replaced and has its output
   discarded by ``persist``. That is what the running-turn refusal in ``Assistant.truncate_conversation``
   keeps out of reach, and ``TODO.md`` carries the deeper fix.
   (Regressions: ``test_proactive_new_session_holds_at_most_one_gate_turn``,
   ``test_delete_does_not_wait_for_a_turn_on_another_conversation``.)

2. **Pin for the whole turn.** The agent registry evicts LRU. Without a pin, another conversation's
   turn can evict this one's agent mid-run, and persisting afterwards would rebuild a stale agent
   from the store and silently lose this turn's output. Pin before the gate, unpin in a ``finally``.

3. **``streaming_conversation`` is set for every turn, unconditionally.** Two readers depend on it:
   the web channel mutes frames from a turn that isn't the conversation being viewed, and the
   approval gate auto-denies a gated tool whose turn isn't being watched. Every channel needs it
   set, the terminal included, so that a turn on the conversation in view reads as foreground rather
   than as a background turn. Note what the terminal's conversation commands (``/new``, ``/switch``)
   made reachable there: a CLI turn the user switched away from now auto-denies a gated tool exactly as
   a web one does, since ``HumanGate.approve`` compares the turn's conversation against the viewed one
   and no longer against the only one there was. The first reader behaves differently between the two,
   and that asymmetry is deliberate: the terminal has no second place to put a background turn's frames,
   so it keeps printing them where the user now is, which is what the command's own reply says.
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
   The terminal's conversation commands add a second way to lose a firing's stop there, and it is a
   known gap rather than a covered case: ``/switch`` mid-firing points ``/stop`` at the conversation
   moved to, and since nothing is muted on that channel the firing keeps printing wherever the user
   went rather than into the conversation it started in. Both of these come from one cause, a channel
   whose ``supports_conversations`` is false sharing the viewed conversation with a scheduled run,
   which is the flag ``_resolve_target`` reads and the follow-up in TODO 12 is about.

   Shutdown is the one reader that must not follow that rule, because it closes the session store.
   Replacing an entry does not end the turn it replaced, so the per-conversation entries are not the
   list of turns still running; ``TurnTracker.live()`` is, and shutdown cancels and awaits that instead.
   A turn left out of it is cancelled by the event loop after the store has closed, and the record
   invariant 5 makes on the way down then raises ``I/O operation on closed file`` out of a task nobody
   is watching, losing that turn's partial answer with it.
   (Regressions: ``test_a_running_firing_is_tracked_under_its_task_so_it_can_be_stopped``,
   ``test_shutdown_cancellation_still_takes_the_firing_down_with_it``,
   ``test_shutdown_waits_for_a_turn_a_later_message_displaced``.)

8. A reactive turn owns its auto-approval budget, opened as ``current_review_context``
   (``core/auto_approval.py``) for the turn's duration and absent outside one. Per turn rather than
   per gate, because concurrent turns on different conversations would otherwise share one counter and
   one conversation's retry loop could spend another's. Absent in an unattended turn on purpose: a
   gated call there is denied before a reviewer is asked, so the missing context is a second, structural
   reason nothing is auto-approved while nobody is watching.

9. **A message typed mid-turn reaches the running turn or becomes the next one, never both and never
   neither.** ``MessageBus.send`` and ``close`` are both synchronous and asyncio is single-threaded,
   so the serve loop cannot observe a bus as open in the same tick this ``finally`` shuts it. What that
   alone does not cover is the window after the loop's last drain: a message accepted there is never
   read, so ``close`` hands it back and this path runs it as a follow-up turn. The "never both" half
   rests on which of the bus's two cursors the entry agent's own run opens: ``close`` measures the
   leftovers from the entry cursor's position, so handing this run a worker's independent source would
   leave that position at zero and re-run every *delivered* message as its own turn. A stop is the
   one exception and is deliberate: someone who cancelled the turn is not asking for one more, so
   those messages are reported as undelivered instead.
   One window is uncovered and known: the re-submit happens after the ``finally`` has released the
   gate, so an exception escaping that block (out of ``_persist``, the gate's ``__aexit__``,
   ``_notify_if_backgrounded``, or a cancellation landing in ``_report_undeliverable``, which swallows
   everything else by design) computes the leftovers and drops them, which is "neither". Moving the
   re-submit into the ``finally`` trades it for two worse faults, awaiting during a cancellation and
   re-running a gate-cancelled turn's messages, so the window stands rather than being closed there.
   Two of those awaits this module added rather than inherited, ``_notify_if_backgrounded`` and
   ``_report_undeliverable``, and the second is why that method catches: an invariant-10 notice must
   not be able to widen invariant 9's window, which is the whole reason it is ordered ahead of the
   re-submit at all.
   A scheduled firing carries the same bus and the same guarantee, and the three places it differs
   all follow from its shape rather than from a different rule. Its bus is opened by
   ``_run_unattended`` rather than by the body that reads it, unlike the ``current_metrics`` scope
   beside it (invariant 3): the body runs in a child task, which copies the context at creation, so a
   contextvar set before the task starts reaches it; and ``close`` has to be reached where the gate
   hold is not held, since the follow-up turn takes a hold of its own and a re-submit from inside the
   body would wait on the per-conversation lock the firing's own hold owns while the firing waits on
   the follow-up (invariant 1). The uncovered window above is also wider for a firing, because it
   reports a failure rather than catching one: an error on its way out of the hold carries that run's
   leftovers past the re-submit. And a stopped firing says less than a stopped reactive turn: that
   path peeks and tells the user their last message was not delivered, while a firing's stop returns
   from inside the hold and drops its leftovers in silence, even where ``echo_reply`` puts its output
   in front of someone who is reading. The bus does the same thing in both cases, which is to drop
   those messages rather than run them; only the sentence about it is missing.
   (Regressions: ``test_a_message_the_entry_agent_never_read_runs_as_a_follow_up_turn``,
   ``test_the_entry_agents_run_opens_the_conversations_own_cursor``,
   ``test_a_stopped_turn_does_not_resubmit_its_undelivered_messages``,
   ``test_a_user_message_no_reader_matched_still_runs_as_a_follow_up_turn``,
   ``test_a_message_an_unattended_firing_never_read_runs_as_a_follow_up_turn``.)

10. **A message addressed to an agent is delivered to a matching reader at its next round boundary,
    or reported undeliverable because no matching reader took it, never both and never neither.**
    Note which word does the work. ``close`` splits by who a message was *from*, not by who it was
    for: the user's own words can become a turn whatever they were addressed to, so a user message to
    a run nothing answered for is re-submitted rather than reported, and is invariant 9's business
    instead of this one. What cannot become a turn is an *agent's* message, because re-running one
    worker's note to another as a user turn would put words in the user's mouth. So this invariant
    governs an agent's message, invariant 9 governs the user's, and the fallback here is a sentence
    rather than a turn.
    Read "a matching reader" strictly, because the obvious stronger reading is not what holds. A bare
    label naming several runs is satisfied by **any one** of them: the drain record is per message
    rather than per address, so a message to ``researcher`` that one of two researchers drained is
    delivered and is not reported, though the second never saw it. The stronger rule would need to
    know which addresses are still running, and nothing in AIMU's protocol says that (see
    ``MessageBus._register``), so there is no honest version of it. This is also the bound on what a
    sender may be promised: a receipt can say a message was accepted and, later, that nothing took
    it, never that a particular run read it.
    The bus records which messages a reader drained, which is what lets ``close`` tell an undelivered
    message from a delivered one rather than inferring it from a cursor position. A position cannot
    carry that fact: every cursor advances past every message whether its filter matched or not, so a
    message addressed to a run that never drained it is passed over by each reader in turn and looks
    read from all of their positions. ``close`` therefore returns two lists, the user's messages to
    re-run and the agents' to report, and this path reports the second before running the first, so
    the notice reads ahead of the follow-up turn's own output.
    **Where the report goes follows from who is watching, not from which path ran.** The report is
    display-only and lands after ``_persist``, so it is never stored: sending it to a channel that is
    showing a different conversation does not misplace it, it destroys it, because
    ``streaming_conversation`` has already been reset by the ``finally`` and the channel can no longer
    tell that this turn was backgrounded. So ``_report_undeliverable`` compares the turn's
    conversation against the active one itself and raises a ``ChannelUI.alert`` instead of sending
    when they differ, which is the same test ``_notify_if_backgrounded`` makes and the opposite branch
    of it. An alert rather than a log, because a log is only right on a channel with somewhere else to
    put a backgrounded turn's output and ``CLIChannel`` has nowhere: it does not mute, so its user is
    reading the terminal that turn is printing to, and a log would make it the one shipped front end
    where this sentence vanished. ``alert`` is the method written for that split, printing where there
    is no card surface. One rule covers both paths: a scheduled firing is backgrounded by construction
    (invariant 4 leaves the active pointer alone), so it alerts, except on a channel with no
    conversation list, where the firing shares the viewed conversation and the user is reading it
    after all.
    Two gaps are known rather than covered, and both are about the *sentence* rather than about the
    pair of them, which is why the two halves of this invariant are stated separately below. The
    window invariant 9 names, an exception escaping the outer ``finally``, applies here too, and this
    notice is an await inside it, which is why it catches rather than raises. A stopped turn reports
    nothing, because the report sits after that ``finally`` rather than inside it, the cancelled branch
    returns before reaching it, and it cannot be moved in, being an await inside a cancellation
    (invariant 9's own argument). So a stop is the same deliberate exception invariant 9 makes for a
    user's message, one shade worse: there, the stop notice at least says a message was not delivered.
    A firing whose own run *failed* loses the sentence the same way and for the same reason, since the
    error raises past the report on its way to ``proactive``: named here rather than left for a reader
    to infer from the record paragraph below, which is where that case is established and which is a
    worse place to meet it than the list of what this invariant does not cover.
    What neither a stop nor a failure loses is the record, for the reason that paragraph gives.
    A message to an address that never existed this turn needs nothing extra: no reader can match it,
    so it is reported here like any other undeliverable one. Refusing it at send time would serve the
    sender better, and the roster can answer that much without tracking liveness, but it belongs with
    whatever lets a sender write an address rather than here.
    **The live report above is not the only one: ``_record_undelivered`` persists the same list
    (``ConversationBook.record_undelivered``) so a reload shows what the live sentence otherwise loses
    the moment it scrolls off. It is written from inside the outer ``finally`` on both paths, as the
    statement after the ``bus.close()`` that discovers what there is to write, and that placement is
    the one thing about this pair that is not a free choice: every ending has to reach the record,
    including the three the report cannot.** A reactive stop returns from the cancelled branch; a
    firing's stop returns from inside its own gate hold, and a firing's failure raises out of it. The
    report cannot follow the record in, being an await inside a cancellation, while this write is
    synchronous, which is the whole of why one half of the pair moved and the other stayed where it is.
    So the two halves answer differently: the sentence is conditional on how the turn ended, and the
    record is not. A firing needs one thing more, because its index is resolved inside a child task
    that raises on two of its three endings and so can hand nothing back by returning:
    ``_unattended_body`` publishes it instead (see :class:`_PublishedIndex`), which is the same answer
    the workflow branch of ``reactive`` already reaches for with ``WorkflowContext.publish_user_index``.
    Both calls catch, for two reasons rather than one: a store error must not replace a cancellation
    propagating out of that ``finally``, and must not skip the teardown below it, which would leave the
    turn's pin and its contextvars set for the life of the process.
    **The write still takes no hold of its own, so the delete it can race is still
    :meth:`ConversationBook.record_undelivered`'s own guard to refuse.** It runs after this
    conversation's gate hold has released and before its pin does, which is nearer than the old
    placement after ``_notify_if_backgrounded``'s await and is not the same thing as safe:
    ``TurnGate.turn``'s exit releases this conversation's lock and *then* re-acquires the gate's shared
    condition, and that re-acquire suspends whenever another conversation's turn is finishing in the
    same instant, which is a yield point a waiting ``delete_conversation`` can run on before this
    ``finally`` executes at all. By contrast ``_record_provenance`` and every call it makes into
    ``record_turn_provenance`` run from inside the gate hold, which is why those never had this
    exposure. So the guard (checking :meth:`ConversationBook.exists` before reading) is what closes
    the window, not a second gate hold taken here -- the store is synchronous throughout (by
    ``aimu.sessions``'s own contract), so the check and the write that follows it cannot be
    interleaved by anything else on the event loop, and a hold would only re-open a second way of
    saying the same thing. Contrast the resubmit call below this block in ``reactive``: that one *does*
    re-take this conversation's gate, because it calls ``self.reactive(...)`` again, a whole new turn
    rather than one store write, and a second, sequential ``gate.turn`` taken only after the first has
    fully released is not the nested case invariant 1 forbids (``_prune_task_conversations`` taking
    ``delete``'s own hold, sequentially, after ``_run_unattended`` has returned, is the same shape). So
    the record and the resubmit are safe for two different reasons, and neither reason transfers to the
    other: the resubmit's safety is mutual exclusion, and the record's is a single-shot check on an
    already-atomic write.
    (Regressions: ``test_close_reports_an_undelivered_agent_message_rather_than_resubmitting_it``,
    ``test_a_message_every_cursor_passed_over_is_reported_rather_than_lost``,
    ``test_a_bare_label_one_of_two_readers_drained_is_delivered_not_reported``,
    ``test_an_undelivered_agent_message_is_reported_and_not_rerun``,
    ``test_a_backgrounded_turns_report_is_logged_rather_than_sent_to_the_wrong_conversation``,
    ``test_a_channel_that_cannot_take_the_report_still_leaves_the_resubmit_to_run``,
    ``test_a_stopped_turn_still_records_an_undelivered_agent_message``,
    ``test_a_stopped_firing_still_records_an_undelivered_agent_message``,
    ``test_a_delete_racing_the_undelivered_record_does_not_resurrect_the_conversation``.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Optional, Union

from aimu import PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.aio import ModelConnectionError, ModelRefusalError, RunHandle
from aimu.aio.channels.base import ChannelMessage
from aimu.sessions import SessionSummary

from kokua.channels.web import proactive_turn, streaming_conversation
from kokua.config.file import thinking_request
from kokua.core.auto_approval import ReviewContext, current_review_context
from kokua.core.build import model_label
from kokua.core.errors import describe_error
from kokua.core.messages import PROVENANCE_MIXED, derive_title, resolve_message_indices, resolve_user_index
from kokua.core.messaging import ENTRY_SOURCE, Message, MessageBus, current_bus
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


@dataclass
class _PublishedIndex:
    """Where an unattended turn's own user message landed, handed back by a body that may raise.

    A return value reaches ``_run_unattended`` on one of ``_unattended_body``'s three endings only:
    a stop and a failure both raise, and an exception carries no return value with it. The caller
    needs the index on all three, because what the turn's bus hands back when it closes has to be
    keyed under that index whatever the ending was, and the caller never sees ``agent.model_client``
    to resolve it for itself. So the body publishes it as it commits, which is the same answer the
    workflow branch of ``reactive`` already reaches for with ``WorkflowContext.publish_user_index``,
    for the identical reason.

    ``-1`` is the honest starting value and the one the store reads as "no turn to key this under"
    (:meth:`ConversationBook.record_undelivered`), so a run that raised before committing anything
    records nothing rather than recording against the wrong turn.
    """

    value: int = -1


def _holds_no_report(session: SessionSummary) -> bool:
    """Whether a task's conversation holds nothing the user would keep over another run's output.

    The retention order in :meth:`TurnRunner._prune_task_conversations` reads this: a run that has no
    report to show is what a cap evicts first, so a task that fails repeatedly cannot cost the user
    the last firing that actually produced something.

    Two ways to hold nothing, because a recorded failure only covers one of them. The reason is keyed
    to the turn's user message, so a firing that raised before its user turn reached the transcript --
    an agent that would not build, a client that failed to construct -- has no turn to key one to. An
    empty transcript says the same thing on its own, which is why this reads ``message_count`` rather
    than ``messages``: ``_prune_task_conversations`` calls this over ``ConversationBook.sessions_for_task``,
    which now hands back summaries rather than whole sessions, and a summary has no ``messages`` to be
    empty.
    """
    return bool(session.metadata.get("failure")) or session.message_count == 0


def _describe_refusal(exc: ModelRefusalError, subject: str) -> str:
    """The fragment every refusal message is built from: "declined <subject> (<category>): <why>".

    Not in ``core/errors.py`` with ``describe_error``, which is deliberately provider-agnostic
    (it inspects only the exception chain, never a library's types) and would stop being so the
    moment it read the two attributes only AIMU's refusal carries. ``category`` is the provider's
    own classifier label and leads, because it is the part that tells a user which rephrasing
    might work; both it and ``explanation`` are routinely absent, so neither may reach the text
    as a literal "None".
    """
    label = f" ({exc.category})" if exc.category else ""
    detail = f": {exc.explanation}" if exc.explanation else "."
    return f"declined {subject}{label}{detail}"


#: How much of one undelivered message's text the notice and the record keep. The same number
#: ``core/transcripts.py`` caps a single message at on read, because this is the same kind of payload:
#: the words of a message, where that module's cap exists so one pasted document cannot consume a read.
#: ``core/subagents.py`` spills past 4,000 characters into a payload file instead, which is the right
#: trade for a tool response somebody may need whole and the wrong one here, where the text is one
#: line of context inside a sentence about something that did not happen.
UNDELIVERED_TEXT_CHARS = 2_000


def _capped_message_text(text: str) -> str:
    """One undelivered message's text, cut with a note saying how much is missing.

    Both surfaces this feeds take it: the live notice and the stored record, which is the one
    model-authored record on this feature that followed none of this codebase's own caps. The record
    is why the cap exists rather than the notice. A notice scrolls away, while
    ``session.metadata["undelivered"]`` is durable, and the text in it is written by a model
    (``toolsets/messaging.py``'s ``send_message``) behind nothing but a non-blank check, where a
    mid-turn message the *user* typed is self-limiting.

    The note is the point, as it is in ``transcript_export``'s own ``_capped``: a silent cut reads as
    a complete record of a short message, so a reader cannot tell the thing they are judging was
    abridged. Worded like ``core/transcripts.py``'s, so one idiom covers both.

    What this does not bound is how many messages one turn can leave undelivered, and that is left
    uncapped deliberately rather than overlooked: a sender spends one of its own permitted rounds per
    send, so the count is already bounded by the round budgets of the runs doing the sending, and the
    record's worst case is that many messages at this cap. Capping the list as well would mean
    dropping whole messages, which needs a second note saying so, for a bound the loop already gives.
    """
    if len(text) <= UNDELIVERED_TEXT_CHARS:
        return text
    return f"{text[:UNDELIVERED_TEXT_CHARS]}... [message truncated, {len(text)} chars total]"


class TurnRunner:
    def __init__(
        self,
        book,
        ui,
        gate,
        config,
        *,
        tracker,
        decide,
        push_conversations,
        delete_conversation,
        spawn_title,
        state=None,
    ):
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
        # Starts the background write of a generated title, once a conversation has derived its
        # placeholder one. The assistant's, because the task outlives this turn and shutdown has to be
        # able to end it; all this module knows is the moment a conversation first gets a title.
        self._spawn_title = spawn_title
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
        # The front end's own id for the message it drew for this turn, read from the same metadata and
        # echoed back when the turn reaches the store, which is how a front end matches the two. Named
        # apart from the contextvar reset token below, which is a different thing entirely.
        bubble_token = (msg.metadata or {}).get("token")
        self._book.pin(conversation_id)  # invariant 2
        token = streaming_conversation.set(conversation_id)  # invariant 3
        collector_token = subagent_events.set([])
        metrics = TurnMetrics()
        metrics_token = current_metrics.set(metrics)
        # The turn's auto-approval budget and request text, opened here so a gated tool call reached
        # from anywhere inside the turn can find both. Reactive only: an unattended turn auto-denies a
        # gated call before a reviewer is ever consulted, so a budget there would never be spent.
        review_context = ReviewContext(request=msg.text)
        review_token = current_review_context.set(review_context)
        # The turn's message bus, open for its whole life so a message typed while it runs can
        # reach it rather than queuing behind it on the gate (invariant 9). Published before the first
        # `await` for a reason the contextvars above do not share: the serve loop reads this entry to
        # route a message, so every statement between its `add` and this line is a window in which a
        # live entry has no bus. Keeping the window at zero statements is not possible from here:
        # a message already in the channel's inbound queue when this turn was submitted is drained in
        # the same loop step and does read the entry before this line runs, which is why
        # `Assistant._offer_message` refuses an entry whose bus is absent. A late *reader* is
        # harmless by contrast, since the bus is append-only and a cursor opened afterwards still
        # sees what was sent before it.
        # Carries `review_context` rather than reading the contextvar, because a send arrives on the
        # serve loop's own task while that contextvar is set inside this turn's: invisible from there.
        bus = MessageBus(review_context=review_context)
        bus_token = current_bus.set(bus)
        # Recorded on the tracker entry the serve loop already added for this conversation, so routing
        # can find this turn's bus by conversation id alone.
        self._tracker.attach_bus(conversation_id, bus)
        # The client carries the forwarder, not this turn's accumulator: the forwarder holds no turn
        # state, so it is safe as the durable client-wide setting AIMU calls it, and the contextvar
        # above is what keeps concurrent turns on other conversations out of this record. Assigned
        # here rather than at agent build time only because a test may inject its own client.
        agent.model_client.events = record_event
        # Record this turn's output for a user who switches into its conversation before it finishes;
        # ended below by `_persist` (and again in the `finally`, for a turn that never got that far).
        self._ui.begin_catch_up(conversation_id, msg.text, msg.images)
        succeeded = False
        # Deliberately belt-and-braces: the `CancelledError` branch below `return`s before reaching the
        # re-submit block at the end of this method, so `undelivered and not stopped` is never evaluated
        # on that path today. The flag is what keeps a stopped turn's leftovers from re-running even if a
        # later refactor removes that early `return`, rather than relying on this shape never changing.
        stopped = False
        failure_reason = ""  # set on error, so a backgrounded turn's notification can carry the reason
        # Where this turn's sub-agent cards anchor, and where the messages the user sent into the turn
        # ended up. Both branches settle both in a `finally`, so the cancellation and error paths below
        # record under the index a completed turn would use; -1 means the turn committed no user
        # message, and recording no-ops.
        user_index = -1
        message_indices: list[int] = []
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
                            # What this finds depends on how the workflow committed, which is why it
                            # is asked through the same helper as the plain branch rather than
                            # short-circuited here. On the shipped `[planning]` defaults
                            # (`result_review` and `show_reasoning` both off) a `/plan` turn executes
                            # through `_execute_streaming`, which keeps the executor's own messages
                            # and rewrites only the prompt, so a message delivered during execution
                            # is in this list and records exactly as a plain turn's does. Turning
                            # either flag on takes `_execute_with_review`, which replaces everything
                            # the executor appended with one user/assistant pair, leaving nothing
                            # here to find and `[]` recorded. Either way the planner's own rounds are
                            # rolled back (`_make_plan`), so a message delivered while the plan was
                            # being drafted shows live, reaches the catch-up record a switch-in
                            # replays, and is gone on reload.
                            message_indices = resolve_message_indices(agent.model_client.messages, user_index)
                            # The rollback just described is exactly the case this cannot pair a
                            # delivery with its message in; see `_tag_agent_messages` for what it
                            # does instead.
                            self._tag_agent_messages(agent, bus, message_indices)
                    else:
                        # Taken here rather than before the gate: it is the lower bound the turn's own
                        # user message is searched for from, so anything another turn appended first
                        # has to be behind it.
                        base_len = len(agent.model_client.messages)
                        try:
                            stream = await agent.run(
                                msg.text,
                                stream=True,
                                images=msg.images,
                                thinking=thinking,
                                inbox=ENTRY_SOURCE,
                            )
                            await self._ui.send(stream, reply_to=msg)
                        finally:
                            # Resolved after the run, not taken as base_len itself: the user message
                            # this anchors to does not exist until the run appends it, and a first turn
                            # seeds the system message ahead of it. Reached on the cancelled path too,
                            # where the agent has already snapshotted the partial turn in its finally.
                            user_index = resolve_user_index(agent.model_client.messages, base_len)
                            message_indices = resolve_message_indices(agent.model_client.messages, user_index)
                            # Before `_persist` writes these messages to the store, which is what the
                            # tag has to reach; see `_tag_agent_messages`.
                            self._tag_agent_messages(agent, bus, message_indices)
                except asyncio.CancelledError:
                    # `/stop` (or shutdown) cancelled this turn. Record first: the "(stopped)" send
                    # below is one more await, and a second cancellation racing it would otherwise
                    # propagate straight past a record placed after -- see invariant 5. Keep the
                    # partial state (the agent snapshots it in a finally), and return so the daemon
                    # keeps serving.
                    stopped = True
                    self._record_provenance(
                        conversation_id,
                        user_index,
                        thinking=thinking,
                        metrics=metrics,
                        started=started,
                        message_indices=message_indices,
                    )
                    logger.info("turn %s cancelled after %.1fs", tid, time.monotonic() - started)
                    # A stop does not re-submit (invariant 9's one exception), so anything the entry
                    # agent had not yet read needs to be said rather than silently run or silently
                    # dropped. `peek_undelivered` rather than `close`: `close` still runs in the
                    # `finally` below and must still see those messages to decide what to hand back.
                    pending = bus.peek_undelivered()
                    notice = "(stopped)" if not pending else "(stopped; your last message was not delivered)"
                    try:
                        await self._ui.send(notice, reply_to=msg)
                    except Exception:
                        pass
                    await self._persist(conversation_id, user_index, token=bubble_token)
                    return
                except ModelConnectionError as exc:
                    # before the send: invariant 5
                    self._record_provenance(
                        conversation_id,
                        user_index,
                        thinking=thinking,
                        metrics=metrics,
                        started=started,
                        message_indices=message_indices,
                    )
                    logger.exception("turn %s connection error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"couldn't reach the model server: {describe_error(exc)}"
                    await self._ui.send(
                        f"The request couldn't reach the model server: {describe_error(exc)}", reply_to=msg
                    )
                except ModelRefusalError as exc:
                    # before the send: invariant 5
                    self._record_provenance(
                        conversation_id,
                        user_index,
                        thinking=thinking,
                        metrics=metrics,
                        started=started,
                        message_indices=message_indices,
                    )
                    # info, not exception: the model was reached and answered in the time it took, so
                    # there is no fault here and a stack trace would file one against Kokua.
                    logger.info("turn %s declined by the model after %.1fs", tid, time.monotonic() - started)
                    failure_reason = _describe_refusal(exc, "this request")
                    await self._ui.send(f"The model {failure_reason}", reply_to=msg)
                except Exception as exc:
                    # before the send: invariant 5
                    self._record_provenance(
                        conversation_id,
                        user_index,
                        thinking=thinking,
                        metrics=metrics,
                        started=started,
                        message_indices=message_indices,
                    )
                    logger.exception("turn %s error after %.1fs", tid, time.monotonic() - started)
                    failure_reason = f"failed: {describe_error(exc)}"
                    await self._ui.send(f"Sorry, the request failed: {describe_error(exc)}", reply_to=msg)
                else:
                    self._record_provenance(
                        conversation_id,
                        user_index,
                        thinking=thinking,
                        metrics=metrics,
                        started=started,
                        message_indices=message_indices,
                    )
                    logger.info("turn %s done after %.1fs", tid, time.monotonic() - started)
                    succeeded = True
                await self._persist(conversation_id, user_index, token=bubble_token)
        finally:
            current_metrics.reset(metrics_token)
            current_review_context.reset(review_token)
            current_bus.reset(bus_token)
            resubmit, undeliverable = bus.close()
            # Written here rather than beside the report below, which is what a stopped turn can still
            # be given: the cancelled branch returns before the report, and the report cannot follow it
            # in here, being an await inside a cancellation (invariant 9's own argument), while this
            # write is synchronous and this block runs on every ending. Caught rather than allowed out,
            # for two reasons: a store error must not replace a cancellation propagating out of this
            # `finally`, and must not skip the resets below it, which would leave this turn's pin and
            # contextvars set for the life of the process.
            try:
                self._record_undelivered(conversation_id, user_index, undeliverable)
            except Exception:
                logger.warning("A message no run read could not be recorded", exc_info=True)
            subagent_events.reset(collector_token)
            streaming_conversation.reset(token)
            # Normally already done by `_persist`; this covers a turn that raised before reaching it,
            # whose record would otherwise linger and replay a phantom user bubble on the next switch-in.
            self._ui.end_catch_up(conversation_id)
            self._book.unpin(conversation_id)
        await self._notify_if_backgrounded(conversation_id, succeeded=succeeded, failure_reason=failure_reason)
        # These same messages are already recorded, by the `finally` above: a channel that fails to take
        # the report (swallowed just below) still leaves a reload with something to show, and the record
        # does not depend on the report succeeding, nor the other way around.
        # Ahead of the re-submit, so the notice reads before the follow-up turn's own output rather
        # than after it. Safe in that order only because the report swallows what it can (see
        # `_report_undeliverable`): a channel failing here must not cost the user the turn below.
        await self._report_undeliverable(conversation_id, undeliverable, reply_to=msg)
        if resubmit and not stopped:
            # Accepted by the bus and never read, because the turn ended first. Running them as a
            # follow-up turn is the fallback the design promises: a message is delivered or it is the
            # next turn, never neither (invariant 9). Only the user's own messages are here; an
            # agent's were reported just above, for the reason invariant 10 gives.
            await self._resubmit_messages(conversation_id, resubmit, like=msg)

    async def _report_undeliverable(
        self, conversation_id: str, messages: list[Message], *, reply_to: Optional[ChannelMessage] = None
    ) -> None:
        """Say that an agent's message reached no run, which is the only fate it can be given.

        A user's message gets a turn of its own instead; an agent's cannot, because re-running one
        worker's note to another as a user turn would put words in the user's mouth (invariant 10). So
        saying so is what is left, and the user is who is told: the sender is a model whose run has
        ended, so there is nobody else still in the turn to tell.

        The sender, the selector and the text are all named, because no two of them identify the
        message. The selector is what nothing answered to and the sender is who is now waiting on an
        answer that will not come, which together are the fact worth acting on; the text is what a
        user would otherwise have to ask the assistant to repeat, capped where the record caps it
        (:func:`_capped_message_text`) so one sentence does not become the whole screen.

        **A backgrounded turn raises an alert instead of sending, and the comparison is made here
        rather than left to the channel.** This runs after ``reactive``'s ``finally``, which has
        already reset ``streaming_conversation``, so a channel that mutes background frames can no
        longer tell that this turn was not the one being watched and would show the notice in whatever
        conversation the user has moved to. The notice is display-only and lands after ``_persist``, so
        that is not a misplacement but a loss: it is gone from the conversation it belongs to.
        ``conversation_id`` against ``self._book.active_id`` is the same test
        ``_notify_if_backgrounded`` makes, taken on the opposite branch, since a notice is *for* a
        backgrounded turn and this is *about* the conversation it ran in.

        ``alert`` rather than a log, and that choice is the one with a trap under it. A log would be
        right only on a channel that has somewhere else to put a background turn's output, and
        ``CLIChannel`` does not: it has no notification frame and does not mute, so its user is reading
        the terminal that turn is still printing to, and logging would make this the one shipped front
        end where the sentence disappeared. ``ChannelUI.alert`` is written for exactly that split,
        raising a card where there is a card surface and printing the sentence where there is not,
        which is why the text names the conversation in words (see its docstring). No ``group``:
        ``notify`` groups by conversation so a later completion supersedes an earlier one, and
        superseding is wrong here, since two turns in one conversation each losing a message are two
        things to know rather than one.

        ``Exception`` is caught and ``CancelledError`` is deliberately not: this await sits inside the
        window invariant 9 names, ahead of a re-submit carrying the user's own words into a turn, so a
        channel that cannot take a notice must not be what loses them. A cancellation is the one thing
        that still has to propagate, which leaves exactly the window invariant 9 already describes
        rather than a wider one. The log is what that failure leaves behind, on either branch.
        """
        if not messages:
            return
        lines = "\n".join(
            f"- from {message.sender} to {message.to}: {_capped_message_text(message.text)}" for message in messages
        )
        try:
            if conversation_id == self._book.active_id:
                await self._ui.send(
                    f"(a message this turn's agents sent was not delivered)\n{lines}", reply_to=reply_to
                )
            else:
                title = self._book.get(conversation_id).metadata.get("title") or "a conversation"
                await self._ui.alert(
                    f"In '{title}', a message one of the turn's agents sent was not delivered.\n{lines}",
                    conversation_id=conversation_id,
                )
        except Exception:
            logger.warning("A message no run read could not be reported:\n%s", lines, exc_info=True)

    async def _resubmit_messages(
        self, conversation_id: str, messages: list[Message], *, like: Optional[ChannelMessage] = None
    ) -> None:
        """Run messages the turn never read as an ordinary follow-up turn on the same conversation.

        The several messages become one turn, so only one front-end bubble can carry that turn's
        controls, and it is the first: the same rule a replay follows for a turn that drew several
        bubbles (text and images), and the only one of them whose index would be the turn's own. The
        rest keep the mark saying they belong inside the turn above them (``app.css``'s own words for
        it), which is still true: each was accepted into a run, and the turn that run became is this
        one. Both callers reach here only with something to run, so there is always a first.

        ``like`` is the message the finishing turn was made of, used as the template this one is built
        from so the follow-up keeps that turn's ``sender`` and ``channel``. A bus message carries a
        front-end token but no channel identity (see :class:`kokua.core.messaging.Message`), so those
        two ``ChannelMessage`` fields have no other route back, and they are what a channel routes a
        reply by (``send(reply_to=...)``): inert on every channel in this repository, and not inert by
        definition. ``images`` is cleared because a
        message handed to a running turn is text only, and ``metadata`` is replaced rather than
        inherited so nothing else riding the original (a per-turn reasoning effort, for one) is
        re-applied to a turn the user never asked that of. A scheduled firing passes nothing, because it
        was started by a prompt and not by a message: its follow-up is as anonymous as the firing was.
        """
        template = like if like is not None else ChannelMessage(text="")
        await self.reactive(
            replace(
                template,
                text="\n\n".join(message.text for message in messages),
                images=None,
                metadata={"token": messages[0].token},
            ),
            conversation_id=conversation_id,
        )

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
        a self-contained base-tier turn's own exchange should be persisted is an open product question.

        Nothing here passes ``inbox``, so a base-tier runner does not receive a mid-turn message by
        default. A runner that wants it closes over ``ctx.agent`` and passes ``ENTRY_SOURCE`` to its
        own ``run()`` call, which it can: the symbol is importable from ``kokua.core.messaging``, and
        the cursor it opens belongs to the turn around it rather than to the run. A self-contained
        runner cannot, because there is no agent run for a bus to reach.
        """
        base_len = len(ctx.agent.model_client.messages)
        try:
            stream = await runner.run(msg.text, stream=True, images=msg.images)
            await self._ui.send(stream, reply_to=msg)
        finally:
            ctx.publish_user_index(resolve_user_index(ctx.agent.model_client.messages, base_len))

    @staticmethod
    def _tag_agent_messages(agent, bus: MessageBus, message_indices: list[int]) -> None:
        """Mark the mid-turn messages that came from an agent, so none of them wears the user's role.

        AIMU appends a delivery as a plain ``user`` message with no provenance of its own (its loop
        tags only the nudges it composes itself, because an inbox message is normally the user's), so
        this is the only place a worker's note can be told from something the user typed once the turn
        is over. Written onto the message dict, which is what ``_persist`` snapshots into the session
        store, so it must run before that: called wherever a turn resolves its indices, which on the
        reactive path is the ``finally`` that reaches the cancelled turn too, rather than from the
        outer ``finally`` that closes the bus, which runs after the store has already been written.

        **A delivery is paired with the message it became by position**, because each non-empty entry
        drain becomes exactly one appended message and ``message_indices`` lists those in order. When
        the two counts disagree the pairing is not safe to make, and the ``/plan`` path is where that
        happens: the planner's rounds are rolled back (``workflows/planning``), so a message delivered
        while the plan was being drafted was drained and then had its appended message discarded,
        leaving more deliveries than indices and every surviving pair off by one.

        The fallback answers one question for the whole turn at once, and the guarantee it has to keep
        is that it can only under-tag, never put the agent tag on an index no delivery actually
        produced. That guarantee needs *more* deliveries than indices, not merely an unequal count:
        with fewer deliveries than indices, broadcasting one answer across every index would also
        answer for an index nothing delivered at all, which is a mis-pairing a count mismatch alone
        does not rule out. So the broadcast only runs with deliveries to spare; short of that, nothing
        here is safe to tag and every index is left untagged, under-tagging completely rather than
        guessing. With deliveries to spare, the broadcast asks whether every delivery in the whole turn
        was agent-only and tags every index if so, which still cannot mis-tag: an aggregate answer of
        "mixed" or "user" leaves every index untagged exactly as a mixed single delivery would.

        ``tag_for_delivery`` is what decides each answer, including the mixed delivery that must stay
        untagged; see it for why under-tagging is the safe direction and what covers the gap. That gap
        is not undefended even when untagged, on either side of it: see
        :func:`kokua.core.messaging._for_model` for the half that protects the model reading the turn
        live, and :meth:`kokua.core.messaging.MessageBus.is_mixed_delivery` for the half below, which
        writes :data:`kokua.core.messages.PROVENANCE_MIXED` so a transcript export or a search does not
        sign or count an agent's contributed words as the user's own, without disturbing
        ``tag_for_delivery``'s own answer or ``is_user_turn``'s.
        """
        deliveries = bus.entry_deliveries()
        if len(deliveries) < len(message_indices):
            # Fewer deliveries than appended messages: at least one index was not produced by any
            # recorded delivery, so no single answer can be broadcast across all of them without
            # answering for content that was never there. Leave every index untagged.
            deliveries = []
        elif len(deliveries) > len(message_indices):
            whole_turn = [message for delivery in deliveries for message in delivery]
            deliveries = [whole_turn] * len(message_indices)
        messages = agent.model_client.messages
        for index, delivery in zip(message_indices, deliveries):
            tag = bus.tag_for_delivery(delivery)
            if tag is not None:
                messages[index][PROVENANCE_KEY] = tag
            elif bus.is_mixed_delivery(delivery):
                messages[index][PROVENANCE_KEY] = PROVENANCE_MIXED

    def _record_provenance(
        self,
        conversation_id: str,
        user_index: int,
        failure: Optional[str] = None,
        *,
        thinking: Optional[Union[bool, str]],
        metrics: Optional[TurnMetrics],
        started: Optional[float],
        message_indices: list[int],
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

        ``message_indices`` is where the messages the user sent into this turn landed, resolved by the
        caller after the run for the reason ``resolve_message_indices`` gives. Keyword-only and
        required on the same principle as the two above: a caller that forgot it would record the turn
        as having received no messages, and nothing downstream could tell that apart from a turn that
        genuinely received none.
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
            messages=message_indices,
        )

    def _record_undelivered(self, conversation_id: str, user_index: int, messages: list[Message]) -> None:
        """Persist what one of this turn's own agents sent and no reader took, so a reload still shows it.

        A call of its own rather than one more keyword on :meth:`_record_provenance`, because what it
        has to record does not exist yet when that one runs: ``messages`` here is ``MessageBus.close()``'s
        second list, and ``close`` is only ever reached in the turn's own ``finally``, after every
        ``_record_provenance`` call this turn makes has already returned.

        Converts each ``Message`` to the plain dict :meth:`ConversationBook.record_undelivered` stores,
        rather than handing the dataclass across: that module has no other reason to import
        ``core.messaging``, and the record only ever needs the three fields a reader of it acts on.

        The text is capped on the way in (:func:`_capped_message_text`), which is the one thing this
        conversion does rather than copies, because the words are a model's and this record is durable.
        """
        self._book.record_undelivered(
            conversation_id,
            user_index,
            [
                {"sender": message.sender, "to": message.to, "text": _capped_message_text(message.text)}
                for message in messages
            ],
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
            await self._ui.notify(f"Reply ready in '{title}'.", conversation_id=conversation_id)
        else:
            await self._ui.notify(f"A reply in '{title}' {failure_reason}.", conversation_id=conversation_id)

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
        except ModelRefusalError as exc:  # invariant 6
            logger.info("proactive turn declined by the model")
            report = f"The model {_describe_refusal(exc, 'a scheduled task')}"
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
            await self._report(report, spec)

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

        The fallback is for a channel that pushes no conversation list. That used to mean the terminal
        could not reach a conversation it was not already in; ``/conversations`` and ``/switch`` ended
        that, so the fallback now rests on the narrower fact that such a channel announces a new
        conversation with nothing but a sentence, and a firing given its own would print into whatever
        the user is reading anyway. Flipping the flag is real work rather than a one-line change (``/stop``
        reaches only the viewed conversation, and a firing's frames are not muted there), and it is
        deliberate follow-up: see TODO 12.
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
        # This firing's message bus, opened here rather than inside the body as `current_metrics`
        # is, for two reasons that scope does not have. The child task copies this context when it
        # starts, so a contextvar set before `RunHandle.start` is what carries the bus into the
        # run; and `close` has to be reached outside the gate hold below, since anything the run never
        # read becomes an ordinary turn and that turn takes a hold of its own (invariant 1). No review
        # context: an unattended turn opens none (invariant 8).
        bus = MessageBus()
        bus_token = current_bus.set(bus)
        self._book.pin(conversation_id)  # invariant 2
        # Where this firing's own user message landed, for the `record_undelivered` call in the
        # `finally` below. Published by the body rather than returned, because every one of its three
        # endings needs to reach that call and two of them raise; see :class:`_PublishedIndex`.
        published_index = _PublishedIndex()
        try:
            # Held here rather than in the child so there is exactly one hold for the firing either way
            # (invariant 1). An asyncio lock has no owning task, so releasing it here is sound.
            async with self._gate.turn(conversation_id):  # invariant 1
                handle = RunHandle.start(self._unattended_body(prompt, spec, bus=bus, index=published_index))
                # Tracked inside the gate, for the same reason the catch-up record is opened there: a
                # firing queued behind a turn already running on this conversation would otherwise
                # overwrite that turn's entry, which is the one `/stop` and shutdown reach. A queued
                # firing is therefore not yet stoppable, which is what the tracker's one-entry-per-
                # conversation rule buys. Registered before the first await below, so the entry is in
                # place by the time the child's first statement runs.
                # The bus rides the entry rather than being attached to it afterwards, because
                # this path builds the entry itself: unlike a reactive turn, whose entry the serve loop
                # already added, there is no window here in which a live entry has no bus.
                self._tracker.add(
                    conversation_id,
                    TurnInfo(
                        handle=handle,
                        started=time.monotonic(),
                        preview=prompt[:120],
                        task_id=spec.task_id,
                        bus=bus,
                    ),
                )
                try:
                    # Nothing to take from the result: the index this run's record is keyed under
                    # arrives through `published_index` instead, on every ending rather than on the
                    # one that returns (see :class:`_PublishedIndex`).
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
            # Ahead of the rest of the teardown, as the same pair is in `reactive`: `end_catch_up`
            # calls back into the channel, so a front end raising there would skip the close and leak
            # an open bus for the life of the process, routing every later message into a turn
            # that has already ended.
            current_bus.reset(bus_token)
            resubmit, undeliverable = bus.close()
            # In this block for the reason `reactive` writes it in its own: a stop returns from inside
            # the hold above and a failure raises out of it, so this is the only place a record reaches
            # all three endings. Caught for that method's two reasons, the second of which is the
            # teardown directly below.
            try:
                self._record_undelivered(conversation_id, published_index.value, undeliverable)
            except Exception:
                logger.warning("A message no run read could not be recorded", exc_info=True)
            self._book.unpin(conversation_id)
            # Normally already done by `_persist`; this covers a run that raised before reaching it.
            self._ui.end_catch_up(conversation_id)
            proactive_turn.reset(proactive_token)
            subagent_events.reset(collector_token)
            streaming_conversation.reset(token)
        # These messages are already recorded, in the `finally` above and for the reason it gives.
        # Reached only by a firing that finished: a stop returns before this line and a failure raises
        # past it, which is why the record is the half that had to move and this one did not.
        # The same helper the reactive path uses, and it decides the same way: a firing is
        # backgrounded by construction (invariant 4 leaves the active pointer alone), so this alerts,
        # except on a channel with no conversation list, where the firing shares the viewed
        # conversation and the user is reading it after all. One rule rather than two (invariant 10),
        # and on a channel with no card surface `alert` prints, so the sentence reaches the user there
        # rather than only a log.
        await self._report_undeliverable(conversation_id, undeliverable)
        if resubmit:
            # Accepted by the bus and never read, because the firing ended first, so it runs as a
            # follow-up turn: a message is delivered or it is the next turn, never neither (invariant
            # 9). Reached only by a firing that finished, which is what the two paths that skip it want:
            # a stop returns above, and someone who stopped a firing is not asking for one more turn.
            # A failed firing raises past here instead, so its leftovers are lost, which is the same
            # uncovered window invariant 9 names on the reactive path and wider here, since a firing
            # reports its failure rather than catching it.
            try:
                await self._resubmit_messages(conversation_id, resubmit)
            except Exception:
                # Held here rather than allowed out, where it would be reported as the *firing*
                # having failed and would suppress the announce for a run that finished. `reactive`
                # has already told the user about every failure it caught itself, so what reaches
                # this handler escaped even that, and a log is the honest place for it (invariant 6).
                logger.warning("A message a scheduled firing never read could not be run", exc_info=True)
        return False

    async def _unattended_body(
        self, prompt: str, spec: ProactiveTarget, *, bus: MessageBus, index: _PublishedIndex
    ) -> None:
        """One unattended turn, inside its caller's gate hold. See the module's concurrency invariants.

        Ends cancelled when it was stopped, as a cancelled task should, having first recorded and
        persisted as much of the turn as it got: ``_run_unattended`` is what turns that back into an
        ordinary return.

        ``bus`` is the firing's own, passed rather than read from ``current_bus`` even though the child
        task this runs in does inherit it. The contextvar exists so a tool call anywhere inside the run
        can reach the bus without it being threaded through; this body is not that, it is the caller's
        own code, and a parameter leaves no absent case to guard against.

        ``index`` is where this turn's own user message landed, published as soon as it is resolved
        rather than returned. ``_run_unattended`` is what ``close()``s ``bus`` and so discovers what
        went undelivered, but it is this body that knows where the turn landed in the transcript,
        since the caller never sees ``agent.model_client``; and all three of this body's endings have
        to carry that fact, where a return value carries it on the one that does not raise. See
        :class:`_PublishedIndex`.
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
                # The conversation's own cursor, not a worker's independent one: what a user who
                # switched into this conversation types is the conversation having seen it, and `close`
                # measures the leftovers from this cursor's position.
                reply = await agent.run(prompt, inbox=ENTRY_SOURCE)
                if spec.echo_reply:
                    await self._ui.send(reply)
            except asyncio.CancelledError:
                # Stopped. Handled alongside the failures rather than left to propagate, so the partial
                # turn is recorded and snapshotted the way a failed one is; re-raised below, after that.
                stopped, failure = True, "stopped"
            except ModelConnectionError as exc:
                error, failure = exc, f"couldn't reach the model server: {describe_error(exc)}"
            except ModelRefusalError as exc:
                error, failure = exc, _describe_refusal(exc, "this request")
            except Exception as exc:
                error, failure = exc, f"failed: {describe_error(exc)}"
            # A firing carries a bus too, so one of its own agents can message it, and that message
            # must not be read as the firing's own output: `is_user_turn` treats a proactive tag as a
            # turn somebody took, where the agent tag is exactly what says nobody did. The agent tag
            # survives either order, since this pass assigns where the tagging below defaults, and it
            # goes first because it is the more specific of the two claims. Resolving the indices here
            # rather than below changes nothing about what they find: neither helper reads the tag
            # written after them.
            proactive_index = resolve_user_index(agent.model_client.messages, start)
            # Published the moment it is known, which is ahead of every ending below: the two that
            # raise reach the caller with no return value, and its `record_undelivered` call needs
            # this index on all three (see :class:`_PublishedIndex`).
            index.value = proactive_index
            message_indices = resolve_message_indices(agent.model_client.messages, proactive_index)
            self._tag_agent_messages(agent, bus, message_indices)
            if spec.echo_reply:
                self._tag_echoed_firing(agent.model_client.messages, start, proactive_index)
            # The reason is recorded here rather than left to `_report`, whose status line goes to
            # whichever conversation the user is viewing rather than to this one. Before the persist,
            # and synchronously, for invariant 5's reason.
            self._record_provenance(
                conversation_id,
                proactive_index,
                failure=failure,
                # An unattended run has no message from a user and so carries no request: the configured
                # effort is both what it would run at and what it did.
                thinking=self._config.thinking_for(self._config.entry_agent),
                metrics=metrics,
                started=started,
                message_indices=message_indices,
            )
            await self._persist(conversation_id, proactive_index)
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

    @staticmethod
    def _tag_echoed_firing(messages: list[dict], start: int, prompt_index: int) -> None:
        """Mark the two messages of a firing that landed in the conversation the user is reading.

        Only the echoing path calls this, and that is the whole design. A firing normally mints its
        own conversation, whose session metadata already carries the task's id -- the conversation
        *is* the record that a task produced it, at the granularity that is actually true. There the
        user is not reading along and the finish is announced by a notification, so no individual
        message arrived unasked-for and none is tagged. A channel with no conversation list has
        nowhere to mint, so the firing runs in the conversation the user is looking at, and there the
        prompt nobody typed and the reply echoed back are the only record that anything arrived on
        its own.

        Two messages, not every message the run appended. The tool calls, the tool results, and the
        assistant turns that narrate them are the loop working, and AIMU's contract is that ordinary
        assistant turns carry no provenance at all: absence means "ordinary turn". Tagging the whole
        exchange said of every round what is only true of the last one, so a scheduled task doing a
        dozen rounds replayed as a dozen proactive messages.

        ``setdefault``, not assignment: the agent loop tags the turns it injects itself
        (``continuation``, ``final_answer``), and those tags are how a transcript tells an injected
        nudge from something the user typed. Overwriting them made every nudge replay as a user
        bubble.
        """
        appended = messages[start:]
        prompt = messages[prompt_index] if 0 <= prompt_index < len(messages) else None
        # The answer, when the run produced one: a firing that failed or was stopped mid-loop ends on
        # a tool result instead, and in that case nothing was echoed at anybody.
        push = appended[-1] if appended and appended[-1].get("role") == "assistant" else None
        for message in (prompt, push):
            if message is not None:
                message.setdefault(PROVENANCE_KEY, PROVENANCE_PROACTIVE)

    async def _report(self, text: str, spec: ProactiveTarget) -> None:
        """Raise an unattended run's own status line as an alert, tolerating a channel that cannot
        take it.

        An alert rather than a message in the transcript: the run happened outside whatever the user
        is reading, so a bubble there is a sentence about somebody else's conversation.
        The card is linked to the run's own conversation and grouped by the task, which is
        deliberate: every firing mints a fresh conversation, so grouping by that would leave a card
        per firing for a task that runs all night.

        Nobody is awaiting this turn, so a failed notification must not become the error that takes
        down the scheduler job (invariant 6).
        """
        try:
            await self._ui.alert(text, conversation_id=spec.conversation_id, group=spec.task_id)
        except Exception:
            logger.warning("A scheduled task ran; its notification could not be delivered", exc_info=True)

    async def _persist(self, conversation_id: str, user_index: int, *, token: Optional[str] = None) -> None:
        """Snapshot the turn onto its session, refreshing the sidebar if a title was just derived, and
        publish where the turn starts so a front end can offer an action on it.

        The publication is deliberately after the write and reads the *stored* transcript through
        ``branchable``: a front end offering to branch a turn must never be offered one the store does
        not hold, and an index that names no user turn there (an unattended run whose only user-role
        message was a loop injection, or a turn that committed none at all) is no turn to act on.

        ``token`` rides along so the front end can tell which of the messages it drew this turn was
        made of. It defaults to None for the callers where that is the honest answer: a proactive turn
        and a scheduled firing are nobody's typed message.
        """
        title_derived = self._book.persist(conversation_id)
        # The store now holds what the catch-up record stood in for. Dropped here, between the write and
        # the next await, rather than in the caller's `finally`: a switch-in landing in between would
        # otherwise replay the turn twice, once from history and once from the record.
        self._ui.end_catch_up(conversation_id)
        if title_derived:
            await self._push_conversations()
            # After the push, not instead of it: the truncated title shows now and the generated one
            # replaces it (with a push of its own) whenever the model answers.
            self._spawn_title(conversation_id)
        if self._book.branchable(conversation_id, user_index):
            await self._ui.turn_saved(conversation_id, user_index, token=token)
