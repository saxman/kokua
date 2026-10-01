"""The per-turn steering mailbox: who reads what, and what happens at the edges."""

from __future__ import annotations

import asyncio

from kokua.core.auto_approval import ReviewContext, current_review_context
from kokua.core.steering import (
    ENTRY_STEERING_SOURCE,
    STEERING_SOURCE,
    SteeringMailbox,
    SteeringMessage,
    current_steering,
)


def test_a_reader_sees_messages_offered_before_and_after_it_opened():
    mailbox = SteeringMailbox()
    mailbox.offer("first")
    drain = mailbox.reader()
    mailbox.offer("second")

    assert drain() == ["first", "second"]
    assert drain() == []


def test_each_reader_has_its_own_cursor():
    mailbox = SteeringMailbox()
    entry = mailbox.reader()
    worker = mailbox.reader()
    mailbox.offer("redirect")

    assert entry() == ["redirect"]
    assert worker() == ["redirect"]


def test_a_closed_mailbox_refuses_an_offer():
    mailbox = SteeringMailbox()
    mailbox.close()

    assert mailbox.offer("too late") is False


def test_close_returns_what_the_entry_reader_never_read():
    mailbox = SteeringMailbox()
    entry = mailbox.entry_reader()
    mailbox.offer("read")
    entry()
    mailbox.offer("unread")

    assert [message.text for message in mailbox.close()] == ["unread"]


def test_close_returns_nothing_a_worker_alone_consumed_is_not_counted_as_read():
    # A worker having seen a message is not a substitute for the conversation seeing it.
    mailbox = SteeringMailbox()
    mailbox.entry_reader()
    worker = mailbox.reader()
    mailbox.offer("redirect")
    worker()

    assert [message.text for message in mailbox.close()] == ["redirect"]


def test_a_message_offered_after_the_last_drain_comes_back_from_close():
    # The race the whole design turns on: accepted, never read, so it must run as its own turn.
    mailbox = SteeringMailbox()
    entry = mailbox.entry_reader()
    entry()
    assert mailbox.offer("just missed it") is True

    assert [message.text for message in mailbox.close()] == ["just missed it"]


def test_peek_undelivered_neither_consumes_nor_closes():
    # The property this task's review called out by name: a second peek sees the same thing the
    # first did, and close() afterward still sees it too. A peek sharing entry_reader()'s mutating
    # drain() would pass the notice-text check in test_turns.py while failing this.
    mailbox = SteeringMailbox()
    mailbox.offer("never mind, do the other thing")

    assert [message.text for message in mailbox.peek_undelivered()] == ["never mind, do the other thing"]
    assert [message.text for message in mailbox.peek_undelivered()] == ["never mind, do the other thing"]
    assert [message.text for message in mailbox.close()] == ["never mind, do the other thing"]


def test_the_shared_source_reads_the_contextvar_when_a_reader_is_opened():
    mailbox = SteeringMailbox()
    token = current_steering.set(mailbox)
    try:
        drain = STEERING_SOURCE.reader()
        mailbox.offer("redirect")
        assert drain() == ["redirect"]
    finally:
        current_steering.reset(token)


def test_the_shared_source_outside_a_turn_yields_nothing():
    drain = STEERING_SOURCE.reader()

    assert drain() == []


def test_the_entry_source_opens_the_cursor_close_measures_from():
    # The whole reason there are two sources. A message the entry agent's run read must not come
    # back from close(), or a delivered redirection would also run again as its own turn.
    mailbox = SteeringMailbox()
    token = current_steering.set(mailbox)
    try:
        drain = ENTRY_STEERING_SOURCE.reader()
        mailbox.offer("redirect")
        assert drain() == ["redirect"]
        assert mailbox.close() == []
    finally:
        current_steering.reset(token)


def test_the_worker_source_does_not_advance_the_entry_cursor():
    mailbox = SteeringMailbox()
    token = current_steering.set(mailbox)
    try:
        worker = STEERING_SOURCE.reader()
        mailbox.offer("redirect")
        assert worker() == ["redirect"]
        # Read by a worker, never by the conversation, so it still runs as a follow-up turn.
        assert [message.text for message in mailbox.close()] == ["redirect"]
    finally:
        current_steering.reset(token)


def test_an_offer_amends_the_running_turns_review_context():
    context = ReviewContext(request="find the bug", used=2)
    mailbox = SteeringMailbox(review_context=context)

    assert mailbox.offer("actually, just read the log") is True
    assert "find the bug" in context.request  # amended, not replaced
    assert "actually, just read the log" in context.request
    # The approval budget is not refreshed: the round cap bounds autonomous looping, which a human
    # message ends, while the budget bounds unprompted gated calls, which it does not.
    assert context.used == 2


def test_an_offer_with_no_review_context_still_lands():
    # An unattended turn opens no review context (invariant 8 in `core/turns.py`), so the mailbox has
    # to work with nothing to amend.
    mailbox = SteeringMailbox()

    assert mailbox.offer("redirect") is True
    assert [message.text for message in mailbox.close()] == ["redirect"]


def test_the_amendment_does_not_depend_on_the_offering_tasks_context():
    """The regression this shape exists for: the offer runs on a task that never set the contextvar.

    ``TurnRunner`` sets ``current_review_context`` inside the turn, and the turn runs in a task of its
    own, so ``asyncio.create_task``'s copy of the context keeps that set from ever reaching the serve
    loop where an offer arrives. An amendment reading the contextvar would be a silent no-op in
    production while a test that set the contextvar itself passed, so what is asserted here is that
    the mailbox carries the context across the boundary instead.
    """
    context = ReviewContext(request="find the bug")
    mailbox = SteeringMailbox(review_context=context)

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
        mailbox.offer("use the log instead")
        await running

    asyncio.run(serve_loop())

    assert "use the log instead" in context.request


def test_close_hands_back_the_front_ends_own_id_for_an_undelivered_message():
    """What a front end needs to find the bubble it drew, once the message becomes a turn after all.

    The text cannot name it: two messages can read the same, and a bubble is not addressable by its
    words. Without the id the follow-up turn is one no front end can match to anything it drew, so a
    message that did become a turn is left with none of that turn's controls.
    """
    mailbox = SteeringMailbox()
    entry = mailbox.entry_reader()
    entry()
    mailbox.offer("just missed it", token="b2")

    assert mailbox.close() == [SteeringMessage("just missed it", "b2")]


def test_a_reader_drains_the_text_alone():
    """The drained list goes to AIMU's loop as the prompts for its next round, so a front end's own
    id for a bubble must not reach it."""
    mailbox = SteeringMailbox()
    drain = mailbox.reader()
    mailbox.offer("use the cache", token="b2")

    assert drain() == ["use the cache"]


def test_a_message_offered_without_a_token_has_none():
    """Every channel carrying typed text can steer, and only a front end that draws its own bubbles
    has an id to name one by."""
    mailbox = SteeringMailbox()
    mailbox.offer("use the cache")

    assert mailbox.close() == [SteeringMessage("use the cache", None)]
