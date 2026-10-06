"""Messages the user sends into a turn that is already running.

A turn holds one mailbox for its whole life. AIMU's loop opens a reader over it at the start of
every run inside that turn that was handed a source (the entry agent's own runs, and every worker
whose spec ``core/agents.py`` writes) and drains that reader once per round, so what the user types
reaches the model at its next model call rather than queuing behind the turn on the gate.

Two design points are worth reading before changing anything here.

**Append-only with a cursor per reader, not a queue.** The user's message goes to the entry agent
*and* to every worker a declared agent spawned, so a shared queue would let whichever worker drained
first consume a message the conversation never saw. ("A declared agent" is the limit, not a flourish:
a worker ``toolsets/capabilities.py`` composes per call builds its own spawn tool and is handed no
source, so it cannot be redirected. ``TODO.md`` carries that gap.)

**Nothing here awaits.** ``offer`` and ``close`` are both synchronous, and asyncio is
single-threaded, so there is no interleaving between the moment a turn's ``finally`` shuts the
mailbox and the moment the serve loop asks whether it is open. That is the whole of invariant 9: a
message is accepted and delivered, or refused and run as its own turn, and never both or neither.
A message accepted in the window after the loop's last drain is not lost either, because ``close``
hands it back for the caller to run as a follow-up turn.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from kokua.core.auto_approval import ReviewContext


@dataclass(frozen=True)
class SteeringMessage:
    """One message handed to a running turn: what the user said, and the front end's id for it.

    The id is what a front end that draws its own bubbles matches a message to its fate by, and it has
    to travel this far because a message can meet a further fate here: accepted into the turn, never
    read, and then run as a turn of its own. That follow-up turn's save is the only frame left to name
    the bubble, and the text cannot name it (two messages can read the same). ``None`` where the
    channel draws no bubbles, which is every channel but the web page.
    """

    text: str
    token: Optional[str] = None


class SteeringMailbox:
    """One running turn's pending user messages, with a cursor per reader."""

    def __init__(self, review_context: Optional["ReviewContext"] = None) -> None:
        self._messages: list[SteeringMessage] = []
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

    def offer(self, text: str, *, token: Optional[str] = None) -> bool:
        """Hand a message to the running turn. ``False`` means the turn is gone: run it as a turn.

        ``token`` is the front end's own id for the message, carried for the reason
        :class:`SteeringMessage` gives and never read here. Keyword-only, which is the shape every
        optional opaque id on this feature's surfaces takes (``ChannelUI.steering_taken`` and
        ``turn_saved``, ``RichChannel.send_steering`` and ``send_turn_saved``), so a caller cannot pass
        one by position and an added parameter cannot change what a positional argument means.
        """
        if not self._open:
            return False
        self._messages.append(SteeringMessage(text, token))
        self._amend_review_context(text)
        return True

    def _amend_review_context(self, text: str) -> None:
        """Add this message to what an auto-approval reviewer reads as the turn's request.

        A reviewer judges one gated call's arguments against the request text, so a turn redirected
        mid-run would otherwise have its calls judged against instructions the user has already
        replaced. The context comes off this instance for the reason ``__init__`` gives: a
        contextvar set inside the turn is invisible on the task an offer arrives on.

        **The amendment happens on acceptance, not on delivery**, which is the earlier of the two
        moments a reader might expect. Acceptance is where the text already is, so it costs nothing;
        delivery would mean carrying the text as far as the drain and amending from inside AIMU's own
        loop. The gap between the two only ever makes the reviewer read something
        the user did say and the run has not acted on yet, because this appends and never replaces,
        never touches ``used``, and is reachable only from ``offer``, whose input is the user's own
        words. A message accepted and never drained is the case the gap is visible in, and all it
        leaves behind is a sentence in a context the turn is about to discard.

        What that costs instead is unbounded growth: every accepted message appends to ``request``, and
        nothing trims it, so a turn steered many times sends a reviewer a prompt that keeps getting
        longer. The reviewer's own cost is already uncounted, which
        ``docs/explanation/auto-approval.md`` names as a known limit, so this rides along inside it.

        Only ``request`` is touched. ``used`` is deliberately left alone: the round cap bounds
        autonomous iteration, which a human message ends, while the approval budget bounds how many
        gated calls run without a prompt, which more user text does not make safer. An unattended
        turn opens no context at all (invariant 8 in ``core/turns.py``), so there is nothing to
        amend there.
        """
        if self._review_context is not None:
            self._review_context.request = f"{self._review_context.request}\n\nThe user then said: {text}"

    def reader(self, agent: Optional[str] = None) -> Callable[[], list[str]]:
        """A cursor for one run: the entry agent's, or one spawned worker's.

        Drains the text alone, which is what AIMU's loop takes as the prompt for its next round.

        Opens at zero, so a worker spawned *after* a message was offered still receives it. That is
        deliberate, and it is where this cursor and :meth:`entry_reader`'s differ: the entry cursor
        measures one conversation's progress through the whole turn, while a worker's measures one
        run that did not exist when the earlier messages arrived. Replaying them to it is context
        rather than news, since the entry agent had already read the redirection and written the spawn
        prompt with it in hand, and it is bounded (one round-budget reset per worker, under AIMU's own
        cap on those). Opening at the current length instead would make the mailbox's simplest
        property, append-only with every reader seeing the list, depend on when a reader was opened.

        ``agent`` is the run's own name, passed positionally by AIMU's loop so it can address one run
        rather than every run's drain. Accepted and ignored here: nothing yet reads it, so it is kept
        only to match the shape AIMU's constructor rehearses before a run starts.
        """
        seen = 0

        def drain() -> list[str]:
            nonlocal seen
            pending = self._messages[seen:]
            seen = len(self._messages)
            return [message.text for message in pending]

        return drain

    def entry_reader(self, agent: Optional[str] = None) -> Callable[[], list[str]]:
        """The entry agent's cursor, whose progress decides what ``close`` hands back.

        One per turn rather than one per run, which is the asymmetry :meth:`reader` explains from the
        other side: this position is the conversation's, so every entry-agent run inside a turn shares
        it, where each worker gets a fresh one opened at zero.

        ``agent`` is accepted for the reason :meth:`reader` gives: AIMU passes the run's name
        positionally, and nothing here reads it yet.
        """

        def drain() -> list[str]:
            pending = self._messages[self._entry_seen :]
            self._entry_seen = len(self._messages)
            return [message.text for message in pending]

        return drain

    def close(self) -> list[SteeringMessage]:
        """Shut the mailbox and return what the entry agent never read, oldest first.

        The whole message rather than its text, unlike a reader's drain: the caller runs these as a
        follow-up turn and a front end waiting on each of them needs them named (see
        :class:`SteeringMessage`).
        """
        self._open = False
        undelivered = self._messages[self._entry_seen :]
        self._entry_seen = len(self._messages)
        return list(undelivered)

    def peek_undelivered(self) -> list[SteeringMessage]:
        """What the entry agent has not read yet, without consuming it or closing the mailbox.

        For a stop, where ``close()`` still runs in the turn's ``finally`` right afterwards: the
        cancelled branch needs to know whether to say anything was lost before that happens, and
        advancing the cursor here would make ``close()`` see nothing left to hand back.
        """
        return list(self._messages[self._entry_seen :])


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

    def reader(self, agent: Optional[str] = None) -> Callable[[], list[str]]:
        """Open this run's cursor. ``agent`` is the run's own name, passed positionally by AIMU's
        loop; accepted and ignored here, since nothing yet reads it.
        """
        mailbox = current_steering.get()
        if mailbox is None:
            return lambda: []
        return mailbox.entry_reader(agent) if self._entry else mailbox.reader(agent)


#: Handed to the entry agent's own runs: the conversation's cursor, whose progress decides what
#: ``close`` re-submits. One cursor per *turn*, not per run, so every entry-agent run inside one turn
#: shares it. That is what the planning workflow needs, which makes two on its default path (the
#: planner's and the executor's) and one more for each review round that sends work back: a message
#: one run delivered is not re-submitted by the one after it.
ENTRY_STEERING_SOURCE = _ContextSteering(entry=True)

#: Handed to every worker whose spec ``core/agents.py`` writes: an independent cursor, so a worker
#: seeing a message is not the conversation seeing it.
STEERING_SOURCE = _ContextSteering(entry=False)
