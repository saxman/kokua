"""Messages the user sends into a turn that is already running.

A turn holds one mailbox for its whole life. AIMU's loop opens a reader over it at the start of
every run inside that turn (the entry agent's, and each spawned worker's) and drains that reader
once per round, so what the user types reaches the model at its next model call rather than queuing
behind the turn on the gate.

Two design points are worth reading before changing anything here.

**Append-only with a cursor per reader, not a queue.** The user's message goes to the entry agent
*and* to every live worker, so a shared queue would let whichever worker drained first consume a
message the conversation never saw.

**Nothing here awaits.** ``offer`` and ``close`` are both synchronous, and asyncio is
single-threaded, so there is no interleaving between the moment a turn's ``finally`` shuts the
mailbox and the moment the serve loop asks whether it is open. That is the whole of invariant 9: a
message is accepted and delivered, or refused and run as its own turn, and never both or neither.
A message accepted in the window after the loop's last drain is not lost either, because ``close``
hands it back for the caller to run as a follow-up turn.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from kokua.core.auto_approval import ReviewContext


class SteeringMailbox:
    """One running turn's pending user messages, with a cursor per reader."""

    def __init__(self, review_context: Optional["ReviewContext"] = None) -> None:
        self._messages: list[str] = []
        self._open = True
        # The turn's auto-approval review context, if it opened one, amended by `offer` so a
        # reviewer judges a redirected turn's calls against what the user now wants. Held as a field
        # rather than read from ``current_review_context`` at use time because an offer arrives on
        # the serve loop's task while that contextvar is set inside the turn's own, so it is not
        # visible there.
        self._review_context = review_context
        # How far the entry agent's own cursor has read. The fallback is decided off this one alone:
        # a worker having seen a message is not the conversation having seen it, so a message only a
        # worker consumed still runs as a follow-up turn rather than vanishing into a summary.
        self._entry_seen = 0

    def offer(self, text: str) -> bool:
        """Hand a message to the running turn. ``False`` means the turn is gone: run it as a turn."""
        if not self._open:
            return False
        self._messages.append(text)
        self._amend_review_context(text)
        return True

    def _amend_review_context(self, text: str) -> None:
        """Add this message to what an auto-approval reviewer reads as the turn's request.

        A reviewer judges one gated call's arguments against the request text, so a turn redirected
        mid-run would otherwise have its calls judged against instructions the user has already
        replaced. The context comes off this instance for the reason ``__init__`` gives: a
        contextvar set inside the turn is invisible on the task an offer arrives on.

        Only ``request`` is touched. ``used`` is deliberately left alone: the round cap bounds
        autonomous iteration, which a human message ends, while the approval budget bounds how many
        gated calls run without a prompt, which more user text does not make safer. An unattended
        turn opens no context at all (invariant 8 in ``core/turns.py``), so there is nothing to
        amend there.
        """
        if self._review_context is not None:
            self._review_context.request = f"{self._review_context.request}\n\nThe user then said: {text}"

    def reader(self) -> Callable[[], list[str]]:
        """A cursor for one run: the entry agent's, or one spawned worker's."""
        seen = 0

        def drain() -> list[str]:
            nonlocal seen
            pending = self._messages[seen:]
            seen = len(self._messages)
            return list(pending)

        return drain

    def entry_reader(self) -> Callable[[], list[str]]:
        """The entry agent's cursor, whose progress decides what ``close`` hands back."""

        def drain() -> list[str]:
            pending = self._messages[self._entry_seen :]
            self._entry_seen = len(self._messages)
            return list(pending)

        return drain

    def close(self) -> list[str]:
        """Shut the mailbox and return what the entry agent never read, oldest first."""
        self._open = False
        undelivered = self._messages[self._entry_seen :]
        self._entry_seen = len(self._messages)
        return list(undelivered)


#: The running turn's mailbox, set by ``TurnRunner`` for the turn's duration and None outside one.
#: A contextvar for the reason ``subagent_events`` is one: a spawn's context is copied from the turn
#: that made it, so a worker reaches its own turn's mailbox with nothing threaded through the spawn.
current_steering: ContextVar[Optional[SteeringMailbox]] = ContextVar("current_steering", default=None)


class _ContextSteering:
    """A ``Steering`` source that resolves the running turn's mailbox when a reader is opened.

    A spec is built once at startup and a mailbox exists only while a turn runs, so the spec cannot
    hold a mailbox. It does not need to: AIMU opens a reader at the *start of each run*, which
    always happens inside the turn that owns it, so resolving the contextvar at that moment is
    enough. A run started outside a turn (no mailbox) gets a drain that returns nothing, which is
    what makes this safe to hand to every agent unconditionally.

    ``entry`` picks which of the mailbox's two cursors a run opens, and the distinction is
    load-bearing rather than cosmetic. ``close()`` decides what to re-submit as a follow-up turn
    from the *entry* cursor's position, so if the entry agent's own run opened an independent
    cursor like a worker's, ``_entry_seen`` would never advance and every delivered message would
    also run again as its own turn: the "never both" half of invariant 9, broken in the common case.
    """

    def __init__(self, entry: bool) -> None:
        self._entry = entry

    def reader(self) -> Callable[[], list[str]]:
        mailbox = current_steering.get()
        if mailbox is None:
            return lambda: []
        return mailbox.entry_reader() if self._entry else mailbox.reader()


#: Handed to the entry agent's own runs: the conversation's cursor, whose progress decides what
#: ``close`` re-submits. One cursor per *turn*, not per run, which is why the planning workflow's
#: three entry-agent runs in one turn correctly share it.
ENTRY_STEERING_SOURCE = _ContextSteering(entry=True)

#: Handed to every spawned worker through its spec: an independent cursor, so a worker seeing a
#: message is not the conversation seeing it.
STEERING_SOURCE = _ContextSteering(entry=False)
