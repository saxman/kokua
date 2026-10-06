"""Messages the user sends into a turn that is already running.

A turn holds one bus for its whole life. AIMU's loop opens a reader over it at the start of every
run inside that turn that was handed a source (the entry agent's own runs, and every worker whose
spec ``core/agents.py`` writes) and drains that reader once per round, so what the user types
reaches the model at its next model call rather than queuing behind the turn on the gate.

Two design points are worth reading before changing anything here.

**Append-only with a cursor per reader, not a queue.** The user's message goes to the entry agent
*and* to every worker a declared agent spawned, so a shared queue would let whichever worker drained
first consume a message the conversation never saw. ("A declared agent" is the limit, not a flourish:
a worker ``toolsets/capabilities.py`` composes per call builds its own spawn tool and is handed no
source, so it cannot be redirected. ``TODO.md`` carries that gap.)

**Nothing here awaits.** ``send`` and ``close`` are both synchronous, and asyncio is
single-threaded, so there is no interleaving between the moment a turn's ``finally`` shuts the
bus and the moment the serve loop asks whether it is open. That is the whole of invariant 9: a
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


#: The user's own address, and the only one a front end ever sends under: a channel's typed message
#: always enters the bus as coming from the user, never from an agent.
USER = "user"

#: The selector that reaches every reader on the bus, regardless of label or ordinal.
EVERYONE = "everyone"


@dataclass(frozen=True)
class Message:
    """One message on a turn's bus, and who it is from and for.

    ``to`` is a selector rather than a recipient list: an address for one run, a label for every run
    with that label, or ``EVERYONE``. Matching is a pure function (see ``matches``) so a reader can
    answer "is this mine" without the bus knowing who is reading.

    ``token`` is the front end's own id for the message, carried for the reason the front end needs
    it: the page draws a bubble before it knows which of a message's fates it met. It is what a front
    end that draws its own bubbles matches a message to its fate by, and it has to travel this far
    because a message can meet a further fate here: accepted into the turn, never read, and then run
    as a turn of its own. That follow-up turn's save is the only frame left to name the bubble, and the
    text cannot name it (two messages can read the same). ``None`` where the channel draws no bubbles,
    which is every channel but the web page.
    """

    text: str
    sender: str
    to: str
    token: Optional[str] = None


class MessageBus:
    """One running turn's pending user messages, with a cursor per reader."""

    def __init__(self, review_context: Optional["ReviewContext"] = None) -> None:
        self._messages: list[Message] = []
        self._open = True
        # The turn's auto-approval review context, if it opened one, amended by `send` so a
        # reviewer judges a redirected turn's calls against what the user now wants. Held as a field
        # rather than read from ``current_review_context`` at use time because a send arrives on
        # the serve loop's task while that contextvar is set inside the turn's own, so it is not
        # visible there.
        self._review_context = review_context
        # How far the entry agent's own cursor has read. The fallback is decided off this one alone:
        # a worker having seen a message is not the conversation having seen it, so a message only a
        # worker consumed still runs as a follow-up turn rather than vanishing into a summary.
        self._entry_seen = 0
        # Every address that has opened a reader this turn, in the order they opened, and the next
        # ordinal per label. Append-only and never retired: nothing in AIMU's protocol signals that a
        # run ended, and inferring it from reader-open order is the identity-by-ordering the protocol
        # itself warns against. So an address names a run that opened a reader, not one still open,
        # and `close` is what reports a message nobody drained.
        self._roster: list[str] = []
        self._ordinals: dict[str, int] = {}

    def roster(self) -> list[str]:
        """Every address that has opened a reader this turn, oldest first."""
        return list(self._roster)

    #: AIMU names a spawned worker ``subagent-{agent_type}``, which is its own decoration of the
    #: agent_type Kokua declared. Addresses follow the config's vocabulary instead, so a sender types
    #: the name it can see in `[agents.<name>]`. Pinned as a fact about the release in
    #: ``tests/test_aimu_compat.py``, since a change to it upstream would silently rename every
    #: worker address here.
    _AIMU_WORKER_PREFIX = "subagent-"

    def _register(self, label: Optional[str], *, ordinal: bool) -> Optional[str]:
        """Mint and record this run's address, or None for a run with no name to address.

        ``ordinal`` is False for the entry agent, of which exactly one runs per turn, so a worker can
        address its parent by the name the config gave it rather than by a counter it cannot know.

        A worker's label arrives carrying AIMU's prefix and is stripped to the declared name. The
        strip is unconditional, so a declared agent literally called ``subagent-foo`` would collapse
        to ``foo``; that edge is accepted rather than guarded, because asking which declared names
        exist would mean this module reaching into the config at reader-open time to serve a case
        nobody hits.
        """
        if label is None:
            return None
        if ordinal and label.startswith(self._AIMU_WORKER_PREFIX):
            label = label[len(self._AIMU_WORKER_PREFIX) :]
        if not ordinal:
            address = label
        else:
            self._ordinals[label] = self._ordinals.get(label, 0) + 1
            address = f"{label}#{self._ordinals[label]}"
        self._roster.append(address)
        return address

    def send(self, text: str, *, sender: str, to: str, token: Optional[str] = None) -> bool:
        """Hand a message to the running turn. ``False`` means the turn is gone: run it as a turn.

        ``sender`` is the address the message is from and ``to`` is the selector it is for (an
        address, a label, or ``EVERYONE``); see :class:`Message` for what each means and how a
        reader will match against ``to``. ``token`` is the front end's own id for the message,
        carried for the reason :class:`Message` gives and never read here. Keyword-only, which is the
        shape every optional opaque id on this feature's surfaces takes (``ChannelUI.message_taken``
        and ``turn_saved``, ``RichChannel.send_message_frame`` and ``send_turn_saved``), so a caller
        cannot pass one by position and an added parameter cannot change what a positional argument
        means.
        """
        if not self._open:
            return False
        self._messages.append(Message(text, sender, to, token))
        self._amend_review_context(text)
        return True

    def _amend_review_context(self, text: str) -> None:
        """Add this message to what an auto-approval reviewer reads as the turn's request.

        A reviewer judges one gated call's arguments against the request text, so a turn redirected
        mid-run would otherwise have its calls judged against instructions the user has already
        replaced. The context comes off this instance for the reason ``__init__`` gives: a
        contextvar set inside the turn is invisible on the task a send arrives on.

        **The amendment happens on acceptance, not on delivery**, which is the earlier of the two
        moments a reader might expect. Acceptance is where the text already is, so it costs nothing;
        delivery would mean carrying the text as far as the drain and amending from inside AIMU's own
        loop. The gap between the two only ever makes the reviewer read something
        the user did say and the run has not acted on yet, because this appends and never replaces,
        never touches ``used``, and is reachable only from ``send``, whose input is the user's own
        words. A message accepted and never drained is the case the gap is visible in, and all it
        leaves behind is a sentence in a context the turn is about to discard.

        What that costs instead is unbounded growth: every accepted message appends to ``request``, and
        nothing trims it, so a turn carrying many mid-turn messages sends a reviewer a prompt that keeps
        getting longer. The reviewer's own cost is already uncounted, which
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

        Opens at zero, so a worker spawned *after* a message was sent still receives it. That is
        deliberate, and it is where this cursor and :meth:`entry_reader`'s differ: the entry cursor
        measures one conversation's progress through the whole turn, while a worker's measures one
        run that did not exist when the earlier messages arrived. Replaying them to it is context
        rather than news, since the entry agent had already read the message and written the spawn
        prompt with it in hand, and it is bounded (one round-budget reset per worker, under AIMU's own
        cap on those). Opening at the current length instead would make the bus's simplest
        property, append-only with every reader seeing the list, depend on when a reader was opened.

        ``agent`` is the run's own name, passed positionally by AIMU's loop so it can address one run
        rather than every run's drain. It mints this run's roster address via :meth:`_register` with
        ``ordinal=True``, since AIMU can spawn more than one worker under the same declared name and
        each one opening a reader needs its own address (see :meth:`_register`).
        """
        self._register(agent, ordinal=True)
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

        ``agent`` is the entry agent's declared name, passed positionally by AIMU's loop. It mints the
        turn's entry address via :meth:`_register` with ``ordinal=False``, so exactly one address
        exists per turn no matter how many entry-agent runs share this cursor, and a worker can
        address its parent by the name the config gives it rather than by a counter it cannot know.
        """
        self._register(agent, ordinal=False)

        def drain() -> list[str]:
            pending = self._messages[self._entry_seen :]
            self._entry_seen = len(self._messages)
            return [message.text for message in pending]

        return drain

    def close(self) -> list[Message]:
        """Shut the bus and return what the entry agent never read, oldest first.

        The whole message rather than its text, unlike a reader's drain: the caller runs these as a
        follow-up turn and a front end waiting on each of them needs them named (see :class:`Message`).
        """
        self._open = False
        undelivered = self._messages[self._entry_seen :]
        self._entry_seen = len(self._messages)
        return list(undelivered)

    def peek_undelivered(self) -> list[Message]:
        """What the entry agent has not read yet, without consuming it or closing the bus.

        For a stop, where ``close()`` still runs in the turn's ``finally`` right afterwards: the
        cancelled branch needs to know whether to say anything was lost before that happens, and
        advancing the cursor here would make ``close()`` see nothing left to hand back.
        """
        return list(self._messages[self._entry_seen :])


#: The running turn's bus, set by ``TurnRunner`` for the turn's duration and None outside one.
#: A contextvar for the reason ``subagent_events`` is one: a spawn's context is copied from the turn
#: that made it, so a worker reaches its own turn's bus with nothing threaded through the spawn.
current_bus: ContextVar[Optional[MessageBus]] = ContextVar("current_bus", default=None)


class _ContextSource:
    """An ``Inbox`` source that resolves the running turn's bus when a reader is opened.

    A spec is built once at startup and a bus exists only while a turn runs, so the spec cannot
    hold a bus. It does not need to: AIMU opens a reader at the *start of each run*, which
    always happens inside the turn that owns it, so resolving the contextvar at that moment is
    enough. A run started outside a turn (no bus) gets a drain that returns nothing, which is
    what makes this safe to hand to every agent unconditionally.

    ``entry`` picks which of the bus's two cursors a run opens, and the distinction is
    load-bearing rather than cosmetic. ``close()`` decides what to re-submit as a follow-up turn
    from the *entry* cursor's position, so if the entry agent's own run opened an independent
    cursor like a worker's, ``_entry_seen`` would never advance and every delivered message would
    also run again as its own turn: the "never both" half of invariant 9, broken in the common case.
    """

    def __init__(self, entry: bool) -> None:
        self._entry = entry

    def reader(self, agent: Optional[str] = None) -> Callable[[], list[str]]:
        """Open this run's cursor. ``agent`` is the run's own name, passed positionally by AIMU's
        loop, straight through to :meth:`MessageBus.entry_reader` or :meth:`MessageBus.reader`, which
        mint this run's roster address from it.
        """
        bus = current_bus.get()
        if bus is None:
            return lambda: []
        return bus.entry_reader(agent) if self._entry else bus.reader(agent)


#: Handed to the entry agent's own runs: the conversation's cursor, whose progress decides what
#: ``close`` re-submits. One cursor per *turn*, not per run, so every entry-agent run inside one turn
#: shares it. That is what the planning workflow needs, which makes two on its default path (the
#: planner's and the executor's) and one more for each review round that sends work back: a message
#: one run delivered is not re-submitted by the one after it.
ENTRY_SOURCE = _ContextSource(entry=True)

#: Handed to every worker whose spec ``core/agents.py`` writes: an independent cursor, so a worker
#: seeing a message is not the conversation seeing it.
WORKER_SOURCE = _ContextSource(entry=False)
