"""The ``messaging`` toolset: `send_message` and `list_agents` over the per-turn `MessageBus`.

The bus's own mechanics (rosters, delivery tracking, `close`'s two lists) are covered in
`tests/core/test_messaging.py`; these assert on the tool surface, which is the half a model reads and
the half that decides whether to send at all. `tests/conftest.py`'s `reset_current_address` fixture
resets the `current_address` contextvar around every test in this process, so a test here that wants a
specific starting value sets it explicitly and everything else can assume the clean default.
"""

from __future__ import annotations

from kokua.core.messaging import EVERYONE, MessageBus, current_address, current_bus
from kokua.toolsets.messaging import TOOLSET, list_agents, send_message


def test_send_message_returns_a_receipt_naming_who_it_was_accepted_for():
    bus = MessageBus()
    bus.reader("researcher")
    bus.reader("researcher")
    token = current_bus.set(bus)
    try:
        receipt = send_message("researcher", "use the index")
        assert "researcher#1" in receipt and "researcher#2" in receipt
    finally:
        current_bus.reset(token)


def test_a_sender_is_not_excluded_from_its_own_selector():
    """Self-delivery, pinned on the surface a model actually sends from.

    Documented behaviour rather than a defect: ``everyone`` means every reader and the sender is one.
    What this test exists for is the consequence recorded beside it (``core/messaging.py``'s
    ``WORKER_SOURCE``): every delivery extends the recipient's round budget, so a run delivering to
    itself extends its own. Pinned for both selectors, because the mitigation the design used to
    record against that residual was a refusal of ``everyone`` from an agent, and the exact-address
    case shows why that lever would not have closed it.
    """
    bus = MessageBus()
    drain = bus.reader("researcher")  # mints `researcher#1` and sets `current_address` to it
    token = current_bus.set(bus)
    try:
        assert "researcher#1" in send_message(EVERYONE, "hello all")
        assert "researcher#1" in send_message("researcher#1", "note to self")
    finally:
        current_bus.reset(token)

    assert drain() == ["[message from researcher#1] hello all", "[message from researcher#1] note to self"]


def test_send_message_refuses_an_address_that_never_existed_this_turn():
    # The roster can answer this without liveness, so refusing beats accepting and
    # then reporting nothing delivered.
    bus = MessageBus()
    bus.reader("researcher")
    token = current_bus.set(bus)
    try:
        receipt = send_message("analyst#1", "hello")
        assert "no agent" in receipt.lower()
        assert bus.close() == ([], [])
    finally:
        current_bus.reset(token)


def test_send_message_outside_a_turn_says_so_rather_than_raising():
    assert "no turn" in send_message("researcher", "hello").lower()


def test_send_message_refuses_blank_text_rather_than_accepting_and_losing_it():
    """The same rule `Assistant._offer_message` applies to the user's own blank text, stated here
    explicitly rather than left to `_for_model`'s attribution prefix making the drained text
    non-blank by accident: AIMU discards whitespace at the drain, so an accepted blank message would
    be delivered to nobody and reported undelivered for words that were never there."""
    bus = MessageBus()
    bus.reader("researcher")
    token = current_bus.set(bus)
    try:
        receipt = send_message("researcher", "   ")
        assert "blank" in receipt.lower()
        assert bus.close() == ([], [])
    finally:
        current_bus.reset(token)


def test_list_agents_names_the_roster_and_everyone():
    bus = MessageBus()
    bus.entry_reader("assistant")
    bus.reader("researcher")
    token = current_bus.set(bus)
    try:
        listing = list_agents()
        assert "assistant" in listing and "researcher#1" in listing and "everyone" in listing
        # `user` is a sender, not a selector (see the module docstring), so it
        # must not appear as something `send_message` could be told to reach.
        assert "user" not in listing.lower()
    finally:
        current_bus.reset(token)


def test_list_agents_outside_a_turn_says_so_rather_than_raising():
    assert "no turn" in list_agents().lower()


def test_send_message_to_everyone_is_accepted_even_with_an_empty_roster():
    # `to == EVERYONE` is valid independently of who has opened a reader so far: a broadcast also
    # reaches a worker spawned later in the turn, whose reader opens at zero (see
    # `MessageBus.reader`). Refusing it here for lack of a roster would contradict that.
    #
    # An earlier version of this test only asserted `"no agent" not in
    # receipt.lower()`, which passes under almost any receipt that is not itself the refusal
    # sentence -- including one from a broken implementation that silently dropped the broadcast
    # instead of sending it. The strong check is the bus's own state: the message must actually be
    # there, addressed to everyone, attributed to whoever called this.
    bus = MessageBus()
    current_address.set("assistant")
    bus_token = current_bus.set(bus)
    try:
        receipt = send_message(EVERYONE, "hello, whoever shows up")
    finally:
        current_bus.reset(bus_token)
    assert "accepted" in receipt.lower()
    resubmit, report = bus.close()
    assert resubmit == []
    assert len(report) == 1
    assert report[0].to == EVERYONE
    assert report[0].text == "hello, whoever shows up"
    assert report[0].sender == "assistant"


def test_an_orchestrators_broadcast_reaches_a_worker_it_spawns_afterwards():
    """The one route an orchestrator has to a worker, end to end, and the one the test above stops short
    of.

    That test pins the *send*: a broadcast is accepted with nothing on the roster, because `EVERYONE`
    has nothing to check against. It then closes the bus with no worker ever spawned, so what it
    asserts is the message being *reported undelivered*. This asserts the other outcome, which is the
    one that matters: a parent is blocked for as long as its children run, so it cannot redirect a
    worker it is already waiting on, but a broadcast it sends in an earlier round is still in front of
    that worker's first drain (`MessageBus.reader` opens at zero), attributed to the parent, and
    therefore not reported at all. Worth pinning as one test rather than two, because
    `docs/how-agents-work/agent-messaging.md` teaches it as the documented way across the matrix's
    empty row, and either half alone leaves that claim resting on the other.

    The receipt is checked for what it does *not* promise. "Accepted for assistant" names the roster at
    send time and says nothing about the worker that will actually read it, which is the accept-not-a-
    promise rule pointing the generous way for once.
    """
    bus = MessageBus()
    bus.entry_reader("assistant")  # the orchestrator's own run, which mints `assistant`
    bus_token = current_bus.set(bus)
    try:
        receipt = send_message(EVERYONE, "check the cache first")
    finally:
        current_bus.reset(bus_token)
    assert "Accepted for assistant" in receipt
    assert "researcher" not in receipt

    worker = bus.reader("subagent-researcher")  # spawned a round later, cursor at zero
    assert worker() == ["[message from assistant] check the cache first"]

    resubmit, report = bus.close()
    assert resubmit == []
    assert report == []  # a reader took it, so the third observation point has nothing to say


def test_send_message_attributes_the_sender_from_whichever_run_last_opened_a_reader():
    # `send_message` has no argument naming who is calling it: the sender comes off the
    # `current_address` contextvar `core/messaging.py` sets when a reader opens. Opening "coder"'s
    # reader after "researcher"'s, the way a sequential nested spawn would inside one run (see that
    # contextvar's own comment), leaves it pointing at "researcher#1" -- so a message to "coder" is
    # correctly attributed to the run that is actually making this call, not to "coder" itself.
    bus = MessageBus()
    bus.reader("coder")
    bus.reader("researcher")
    bus_token = current_bus.set(bus)
    try:
        send_message("coder", "status?")
    finally:
        current_bus.reset(bus_token)
    resubmit, report = bus.close()
    assert resubmit == []
    assert len(report) == 1
    assert report[0].sender == "researcher#1"
    assert report[0].to == "coder"


def test_send_message_refuses_when_this_run_has_no_address_of_its_own():
    # A run that never opened a reader (a composed or spawned worker
    # whose spec forgot an inbox; see `core/subagents.py`'s `SubagentReporter` for the other half of
    # the fix) must not be able to send under whoever happens to be sharing its Context. `current_
    # address` is explicitly `None` here -- the state a forgetful worker is actually left in, not
    # merely "untouched" -- and that alone must be refused even though "researcher#1" is a perfectly
    # valid destination on the roster.
    bus = MessageBus()
    bus.reader("researcher")
    current_address.set(None)
    bus_token = current_bus.set(bus)
    try:
        receipt = send_message("researcher", "hello")
    finally:
        current_bus.reset(bus_token)
    assert "no address" in receipt.lower()
    assert bus.close() == ([], [])


def test_toolset_declares_both_tools_and_is_not_entry_point_only():
    # Both permitted directions (the entry agent addressing a worker, a worker answering its parent)
    # need this toolset, which is why it is not restricted the way `skills` is.
    assert TOOLSET.name == "messaging"
    assert TOOLSET.entry_point_only is False
    built = {fn.__name__ for fn in TOOLSET.build(None)}
    assert built == {"send_message", "list_agents"}


def test_list_agents_names_an_address_that_has_already_finished():
    # The roster cannot distinguish a finished run from a running one (see `MessageBus._register`),
    # and `list_agents`'s own docstring says so; this pins that the listing does not pretend
    # otherwise by, say, quietly dropping an address once its one caller stops asking for it. There is
    # nothing here that *could* drop it -- the bus never retires an address -- so this is really a
    # statement that the roster is read as-is.
    bus = MessageBus()
    drain = bus.reader("researcher")
    drain()  # a finished run's last act, in spirit: nothing further distinguishes it from a live one
    bus_token = current_bus.set(bus)
    try:
        assert "researcher#1" in list_agents()
    finally:
        current_bus.reset(bus_token)
