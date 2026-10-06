"""The per-turn message bus: who reads what, and what happens at the edges."""

from __future__ import annotations

import asyncio

from kokua.core.auto_approval import ReviewContext, current_review_context
from kokua.core.messaging import (
    ENTRY_SOURCE,
    EVERYONE,
    USER,
    WORKER_SOURCE,
    Message,
    MessageBus,
    current_bus,
)


def test_a_worker_reader_registers_an_address_under_its_declared_label():
    # AIMU names a spawned worker `subagent-{agent_type}`, so the inbox is told
    # "subagent-researcher" while the config that declared it says [agents.researcher]. Addresses
    # follow the config's vocabulary: a sender types the name it can see.
    bus = MessageBus()
    bus.reader("subagent-researcher")

    assert bus.roster() == ["researcher#1"]


def test_an_agent_whose_declared_name_starts_with_the_prefix_is_an_accepted_edge():
    # Stripping is unconditional, so a declared `subagent-foo` collapses to `foo`. Nobody will name
    # an agent that, and the alternative (asking Kokua which declared names exist, at reader-open,
    # inside the bus) buys nothing for a case that does not arise.
    bus = MessageBus()
    bus.reader("subagent-subagent-foo")

    assert bus.roster() == ["subagent-foo#1"]


def test_two_workers_sharing_a_label_get_distinct_addresses():
    # Review focus 4. Two concurrent researchers must be separately addressable, which is the whole
    # reason an address is minted per reader rather than per agent name.
    bus = MessageBus()
    bus.reader("subagent-researcher")
    bus.reader("subagent-researcher")

    assert bus.roster() == ["researcher#1", "researcher#2"]


def test_the_entry_agent_takes_its_bare_name_with_no_ordinal():
    # Exactly one entry agent runs per turn, so an ordinal would be noise, and a worker can address
    # its parent by the name the config gives it.
    bus = MessageBus()
    bus.entry_reader("assistant")

    assert bus.roster() == ["assistant"]


def test_an_unnamed_run_registers_nothing_rather_than_a_placeholder():
    # Defensive rather than reachable: AIMU's loop permits a None agent name, but `Agent` generates
    # one when a caller supplies none, so Kokua never sees it. One branch, kept because the loop's
    # contract allows it and a placeholder would collide with a real label.
    bus = MessageBus()
    bus.reader(None)

    assert bus.roster() == []


def test_a_message_carries_its_sender_and_its_selector():
    bus = MessageBus()
    drain = bus.entry_reader("assistant")
    bus.send("use the cache", sender="user", to="everyone")

    assert drain() == ["use the cache"]


def test_a_reader_sees_messages_offered_before_and_after_it_opened():
    bus = MessageBus()
    bus.send("first", sender=USER, to=EVERYONE)
    drain = bus.reader()
    bus.send("second", sender=USER, to=EVERYONE)

    assert drain() == ["first", "second"]
    assert drain() == []


def test_each_reader_has_its_own_cursor():
    bus = MessageBus()
    entry = bus.reader()
    worker = bus.reader()
    bus.send("redirect", sender=USER, to=EVERYONE)

    assert entry() == ["redirect"]
    assert worker() == ["redirect"]


def test_a_closed_bus_refuses_a_send():
    bus = MessageBus()
    bus.close()

    assert bus.send("too late", sender=USER, to=EVERYONE) is False


def test_close_returns_what_the_entry_reader_never_read():
    bus = MessageBus()
    entry = bus.entry_reader()
    bus.send("read", sender=USER, to=EVERYONE)
    entry()
    bus.send("unread", sender=USER, to=EVERYONE)

    assert [message.text for message in bus.close()] == ["unread"]


def test_close_returns_nothing_a_worker_alone_consumed_is_not_counted_as_read():
    # A worker having seen a message is not a substitute for the conversation seeing it.
    bus = MessageBus()
    bus.entry_reader()
    worker = bus.reader()
    bus.send("redirect", sender=USER, to=EVERYONE)
    worker()

    assert [message.text for message in bus.close()] == ["redirect"]


def test_a_message_offered_after_the_last_drain_comes_back_from_close():
    # The race the whole design turns on: accepted, never read, so it must run as its own turn.
    bus = MessageBus()
    entry = bus.entry_reader()
    entry()
    assert bus.send("just missed it", sender=USER, to=EVERYONE) is True

    assert [message.text for message in bus.close()] == ["just missed it"]


def test_peek_undelivered_neither_consumes_nor_closes():
    # The property this task's review called out by name: a second peek sees the same thing the
    # first did, and close() afterward still sees it too. A peek sharing entry_reader()'s mutating
    # drain() would pass the notice-text check in test_turns.py while failing this.
    bus = MessageBus()
    bus.send("never mind, do the other thing", sender=USER, to=EVERYONE)

    assert [message.text for message in bus.peek_undelivered()] == ["never mind, do the other thing"]
    assert [message.text for message in bus.peek_undelivered()] == ["never mind, do the other thing"]
    assert [message.text for message in bus.close()] == ["never mind, do the other thing"]


def test_the_shared_source_reads_the_contextvar_when_a_reader_is_opened():
    bus = MessageBus()
    token = current_bus.set(bus)
    try:
        drain = WORKER_SOURCE.reader()
        bus.send("redirect", sender=USER, to=EVERYONE)
        assert drain() == ["redirect"]
    finally:
        current_bus.reset(token)


def test_the_shared_source_outside_a_turn_yields_nothing():
    drain = WORKER_SOURCE.reader()

    assert drain() == []


def test_the_entry_source_opens_the_cursor_close_measures_from():
    # The whole reason there are two sources. A message the entry agent's run read must not come
    # back from close(), or a delivered message would also run again as its own turn.
    bus = MessageBus()
    token = current_bus.set(bus)
    try:
        drain = ENTRY_SOURCE.reader()
        bus.send("redirect", sender=USER, to=EVERYONE)
        assert drain() == ["redirect"]
        assert bus.close() == []
    finally:
        current_bus.reset(token)


def test_the_worker_source_does_not_advance_the_entry_cursor():
    bus = MessageBus()
    token = current_bus.set(bus)
    try:
        worker = WORKER_SOURCE.reader()
        bus.send("redirect", sender=USER, to=EVERYONE)
        assert worker() == ["redirect"]
        # Read by a worker, never by the conversation, so it still runs as a follow-up turn.
        assert [message.text for message in bus.close()] == ["redirect"]
    finally:
        current_bus.reset(token)


def test_a_send_amends_the_running_turns_review_context():
    context = ReviewContext(request="find the bug", used=2)
    bus = MessageBus(review_context=context)

    assert bus.send("actually, just read the log", sender=USER, to=EVERYONE) is True
    assert "find the bug" in context.request  # amended, not replaced
    assert "actually, just read the log" in context.request
    # The approval budget is not refreshed: the round cap bounds autonomous looping, which a human
    # message ends, while the budget bounds unprompted gated calls, which it does not.
    assert context.used == 2


def test_a_send_with_no_review_context_still_lands():
    # An unattended turn opens no review context (invariant 8 in `core/turns.py`), so the bus has
    # to work with nothing to amend.
    bus = MessageBus()

    assert bus.send("redirect", sender=USER, to=EVERYONE) is True
    assert [message.text for message in bus.close()] == ["redirect"]


def test_the_amendment_does_not_depend_on_the_sending_tasks_context():
    """The regression this shape exists for: the send runs on a task that never set the contextvar.

    ``TurnRunner`` sets ``current_review_context`` inside the turn, and the turn runs in a task of its
    own, so ``asyncio.create_task``'s copy of the context keeps that set from ever reaching the serve
    loop where a send arrives. An amendment reading the contextvar would be a silent no-op in
    production while a test that set the contextvar itself passed, so what is asserted here is that
    the bus carries the context across the boundary instead.
    """
    context = ReviewContext(request="find the bug")
    bus = MessageBus(review_context=context)

    async def serve_loop():
        async def turn():
            token = current_review_context.set(context)
            try:
                await asyncio.sleep(0)
            finally:
                current_review_context.reset(token)

        running = asyncio.create_task(turn())
        await asyncio.sleep(0)  # let the turn set its context
        assert current_review_context.get() is None  # and it is still invisible from here
        bus.send("use the log instead", sender=USER, to=EVERYONE)
        await running

    asyncio.run(serve_loop())

    assert "use the log instead" in context.request


def test_close_hands_back_the_front_ends_own_id_for_an_undelivered_message():
    """What a front end needs to find the bubble it drew, once the message becomes a turn after all.

    The text cannot name it: two messages can read the same, and a bubble is not addressable by its
    words. Without the id the follow-up turn is one no front end can match to anything it drew, so a
    message that did become a turn is left with none of that turn's controls.
    """
    bus = MessageBus()
    entry = bus.entry_reader()
    entry()
    bus.send("just missed it", sender=USER, to=EVERYONE, token="b2")

    assert bus.close() == [Message("just missed it", sender=USER, to=EVERYONE, token="b2")]


def test_a_reader_drains_the_text_alone():
    """The drained list goes to AIMU's loop as the prompts for its next round, so a front end's own
    id for a bubble must not reach it."""
    bus = MessageBus()
    drain = bus.reader()
    bus.send("use the cache", sender=USER, to=EVERYONE, token="b2")

    assert drain() == ["use the cache"]


def test_a_message_sent_without_a_token_has_none():
    """Every channel carrying typed text can reach a running turn, and only a front end that draws
    its own bubbles has an id to name one by."""
    bus = MessageBus()
    bus.send("use the cache", sender=USER, to=EVERYONE)

    assert bus.close() == [Message("use the cache", sender=USER, to=EVERYONE, token=None)]
