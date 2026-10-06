"""The ``messaging`` toolset: `send_message` and `list_agents` over the per-turn `MessageBus`.

The bus's own mechanics (rosters, delivery tracking, `close`'s two lists) are covered in
`tests/core/test_messaging.py`; these assert on the tool surface, which is the half a model reads and
the half that decides whether to send at all.
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


def test_send_message_refuses_an_address_that_never_existed_this_turn():
    # Review focus 2. The roster can answer this without liveness, so refusing beats accepting and
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


def test_list_agents_names_the_roster_and_the_user():
    bus = MessageBus()
    bus.entry_reader("assistant")
    bus.reader("researcher")
    token = current_bus.set(bus)
    try:
        listing = list_agents()
        assert "assistant" in listing and "researcher#1" in listing and "everyone" in listing
    finally:
        current_bus.reset(token)


def test_list_agents_outside_a_turn_says_so_rather_than_raising():
    assert "no turn" in list_agents().lower()


def test_send_message_refusal_does_not_merely_say_refused_while_still_sending():
    # Negative control for the refusal test above. A receipt is only proof of a refusal if nothing
    # was actually handed to the bus; a sender-facing sentence is not. If `send_message` sent the
    # text and *also* worded its reply like a refusal, a check on wording alone would still pass. The
    # strong assertion is the bus's own state: `close()` must report nothing and resubmit nothing, or
    # the message went out despite the words saying otherwise.
    bus = MessageBus()
    bus.reader("researcher")
    token = current_bus.set(bus)
    try:
        receipt = send_message("analyst#1", "hello")
        wording_alone_looks_right = "no agent" in receipt.lower()
        resubmit, report = bus.close()
        assert wording_alone_looks_right
        assert resubmit == [] and report == [], "the refusal must mean nothing was sent, not just said"
    finally:
        current_bus.reset(token)


def test_send_message_to_everyone_is_accepted_even_with_an_empty_roster():
    # `to == EVERYONE` is valid independently of who has opened a reader so far: a broadcast also
    # reaches a worker spawned later in the turn, whose reader opens at zero (see
    # `MessageBus.reader`). Refusing it here for lack of a roster would contradict that.
    bus = MessageBus()
    token = current_bus.set(bus)
    try:
        receipt = send_message(EVERYONE, "hello, whoever shows up")
        assert "no agent" not in receipt.lower()
    finally:
        current_bus.reset(token)


def test_send_message_attributes_the_sender_from_whichever_run_last_opened_a_reader():
    # `send_message` has no argument naming who is calling it: the sender comes off the
    # `current_address` contextvar `core/messaging.py` sets when a reader opens. Opening "coder"'s
    # reader after "researcher"'s, the way a sequential nested spawn would inside one run (see that
    # contextvar's own comment), leaves it pointing at "researcher#1" -- so a message to "coder" is
    # correctly attributed to the run that is actually making this call, not to "coder" itself.
    bus = MessageBus()
    address_token = current_address.set(None)
    try:
        bus.reader("coder")
        bus.reader("researcher")
        bus_token = current_bus.set(bus)
        try:
            send_message("coder", "status?")
        finally:
            current_bus.reset(bus_token)
    finally:
        current_address.reset(address_token)
    resubmit, report = bus.close()
    assert resubmit == []
    assert len(report) == 1
    assert report[0].sender == "researcher#1"
    assert report[0].to == "coder"


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
    address_token = current_address.set(None)
    try:
        drain = bus.reader("researcher")
        drain()  # a finished run's last act, in spirit: nothing further distinguishes it from a live one
        bus_token = current_bus.set(bus)
        try:
            assert "researcher#1" in list_agents()
        finally:
            current_bus.reset(bus_token)
    finally:
        current_address.reset(address_token)
