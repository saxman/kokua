"""Per-conversation in-flight turn bookkeeping, replacing the single-turn fields on Assistant.

With concurrent per-conversation turns, the assistant tracks at most one running turn per
conversation (the per-conversation TurnGate lock enforces the "at most one"). This holds each turn's
RunHandle plus the diagnostics the /diag command and the front-end "working" indicator read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from aimu.aio import RunHandle

if TYPE_CHECKING:
    from kokua.core.steering import SteeringMailbox


@dataclass
class TurnInfo:
    handle: RunHandle
    started: float
    preview: str
    # The scheduled task this turn is a firing of, None for a turn the user asked for. Held here rather
    # than read back off the conversation's stored metadata so it is also known for a firing on a channel
    # with no conversation list, which runs in the viewed conversation and stamps no task id on it.
    task_id: Optional[str] = None
    # The turn's steering mailbox, so a message typed while it runs can be routed to it by
    # conversation. None for a turn that predates its creation, which nothing here produces.
    steering: Optional["SteeringMailbox"] = None


class TurnTracker:
    """One entry per conversation, plus the set of every turn actually still running.

    The two answer different questions and cannot be the same structure. ``/stop``, the working
    indicator and ``/diag`` want *the* turn for a conversation, so those read a map that holds one; but
    a turn submitted while another is still running on that conversation replaces the entry without
    ending the turn it replaced, so that map is not the list of what is in flight. Shutdown needs the
    list, since it closes the session store and a turn still running when that happens dies part way
    through the provenance record it makes on its way down, and so does ``running``, whose callers are
    asking whether anything at all is touching a conversation before they mutate it.
    """

    def __init__(self):
        self._turns: dict[str, TurnInfo] = {}
        # A list of ``(conversation id, handle)`` pairs, compared by identity rather than keyed in a set
        # or a dict, so nothing here depends on ``RunHandle`` being hashable. It is a foreign type, its
        # hashability is incidental to being a plain class, and a set would turn a change to it into a
        # runtime failure at the moment a turn starts. The conversation rides alongside each handle so
        # ``running`` can ask "is anything in flight here" of the same list shutdown reads. At most a
        # handful of turns are ever in flight, so the linear scans below cost nothing.
        self._live: list[tuple[str, RunHandle]] = []

    def add(self, conversation_id: str, info: TurnInfo) -> None:
        self._turns[conversation_id] = info
        self._live.append((conversation_id, info.handle))

    def get(self, conversation_id: str) -> Optional[TurnInfo]:
        return self._turns.get(conversation_id)

    def attach_steering(self, conversation_id: str, mailbox: "SteeringMailbox") -> None:
        """Give this conversation's entry the mailbox of the turn publishing one, last write winning.

        Deliberately *not* guarded the way ``remove_if`` is, and the difference is worth the paragraph
        because the obvious guard here is actively harmful. ``remove_if`` can compare a ``RunHandle``;
        a turn publishing its mailbox holds no handle (the serve loop creates it, in ``Assistant``),
        so the only value-level stand-in available is "this entry has no mailbox yet" -- which looks
        like the same protection and is not. Two messages already sitting in the channel's inbound
        queue are drained in a single loop step (``Queue.get`` on a non-empty queue does not suspend),
        and the submit block takes no ``await`` between starting a turn and adding its entry, so both
        ``add`` calls land before either turn's body runs its first statement. Under that guard the
        older turn's body then writes its mailbox into the *newer* turn's fresh entry and the newer
        turn is refused, leaving a closed mailbox on the entry routing reads for as long as the newer
        turn lives: steering silently dead for the turn that is actually running.

        Last write wins instead, because ``RunHandle.start`` defers a body to a later loop step and
        tasks step in creation order, so the newest turn the serve loop submitted is the last to
        publish in every interleaving the loop permits. One writer is not the serve loop:
        ``TurnRunner._resubmit_steering`` runs a follow-up turn from inside the finishing turn's own
        task, and its write winning is also what you want, since its mailbox is the open one. That
        holds for a reactive turn, whose entry the serve loop added and whose done-callback has not
        fired yet; a scheduled firing's follow-up has no entry to win at all, because the firing
        removes its own before the re-submit runs, so this call finds nothing there and a firing's
        follow-up turn cannot itself be steered. What no ordering rule can fix is that one entry
        cannot name two concurrently live turns, which is the same limitation ``running`` documents
        from the other side.
        (Regression: ``test_a_burst_of_two_turns_leaves_the_newer_turns_mailbox_on_the_entry``.)
        """
        info = self._turns.get(conversation_id)
        if info is not None:
            info.steering = mailbox

    def remove_if(self, conversation_id: str, handle: RunHandle) -> None:
        """Remove ``conversation_id``'s entry only when it is the one holding ``handle``.

        A turn's done-callback must not evict a newer turn's entry for the same conversation. The gate
        serializes same-conversation turns but does not stop a second one being *submitted*, so two can
        coexist with only the newer one holding the entry: a finished turn therefore only ever clears
        its own.

        The live list drops the handle unconditionally, keyed by the handle rather than by the
        conversation, precisely because a displaced turn no longer matches the entry. Leaving it behind
        would make shutdown wait on a turn that has already ended."""
        self._live = [(cid, live) for cid, live in self._live if live is not handle]
        info = self._turns.get(conversation_id)
        if info is not None and info.handle is handle:
            del self._turns[conversation_id]

    def running(self, conversation_id: str) -> bool:
        """Whether any turn is still in flight on ``conversation_id``, displaced ones included.

        Read off the live list rather than the one-per-conversation entry, because a displaced turn (one
        whose entry a later turn on the same conversation replaced) is still running while the entry no
        longer names it. A caller asking this is asking whether it is safe to mutate the conversation
        right now, and the honest answer covers every turn touching it, not just the newest: with the
        entry as the source, a newer turn finishing first clears it and reports the older turn's
        conversation as idle, so a destructive edit meant to be refused instead parks on the gate behind
        that turn.

        ``turn_elapsed`` still reads the entry, so in that displaced state a front end can be told a turn
        is running while having no start time to count from. That is the smaller wrong answer of the two:
        a missing clock beside an honest "still running" beats a wedged UI.
        """
        return any(cid == conversation_id and not handle.done for cid, handle in self._live)

    def all(self) -> list[tuple[str, "TurnInfo"]]:
        return list(self._turns.items())

    def live(self) -> list[RunHandle]:
        """Every turn still running, including one whose entry a later turn on the same conversation
        replaced.

        This is what shutdown cancels and waits for. Reading ``all()`` there instead would miss a
        displaced turn, which the event loop then cancels after the session store has closed, and the
        record it makes while unwinding raises out of a task nobody is watching."""
        return [handle for _, handle in self._live if not handle.done]

    def for_task(self, task_id: str) -> list[tuple[str, "TurnInfo"]]:
        """Every still-running turn that is a firing of ``task_id``, as ``(conversation id, info)``.

        A list rather than one entry: a task can have two firings in flight at once (a manual run-now
        alongside its armed one), each in a conversation of its own, so stopping "the task's run" means
        stopping all of them."""
        return [
            (conversation_id, info)
            for conversation_id, info in self._turns.items()
            if info.task_id == task_id and not info.handle.done
        ]
