"""The per-turn steering mailbox: who reads what, and what happens at the edges."""

from __future__ import annotations

from kokua.core.steering import (
    ENTRY_STEERING_SOURCE,
    STEERING_SOURCE,
    SteeringMailbox,
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

    assert mailbox.close() == ["unread"]


def test_close_returns_nothing_a_worker_alone_consumed_is_not_counted_as_read():
    # A worker having seen a message is not a substitute for the conversation seeing it.
    mailbox = SteeringMailbox()
    mailbox.entry_reader()
    worker = mailbox.reader()
    mailbox.offer("redirect")
    worker()

    assert mailbox.close() == ["redirect"]


def test_a_message_offered_after_the_last_drain_comes_back_from_close():
    # The race the whole design turns on: accepted, never read, so it must run as its own turn.
    mailbox = SteeringMailbox()
    entry = mailbox.entry_reader()
    entry()
    assert mailbox.offer("just missed it") is True

    assert mailbox.close() == ["just missed it"]


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
        assert mailbox.close() == ["redirect"]
    finally:
        current_steering.reset(token)
