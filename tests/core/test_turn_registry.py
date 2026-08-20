"""Unit tests for TurnTracker: per-conversation in-flight turn bookkeeping."""

from __future__ import annotations

from types import SimpleNamespace

from kokua.core.turn_registry import TurnInfo, TurnTracker


def _handle(done=False):
    return SimpleNamespace(done=done, cancel=lambda: True)


def test_add_get_and_running():
    tracker = TurnTracker()
    info = TurnInfo(handle=_handle(), started=1.0, preview="hi")
    tracker.add("c1", info)
    assert tracker.get("c1") is info
    assert tracker.running("c1") is True
    assert tracker.get("other") is None
    assert tracker.running("other") is False


def test_running_false_when_handle_done():
    tracker = TurnTracker()
    tracker.add("c1", TurnInfo(handle=_handle(done=True), started=1.0, preview="x"))
    assert tracker.running("c1") is False


def test_all_reports_every_tracked_conversation():
    tracker = TurnTracker()
    tracker.add("c1", TurnInfo(handle=_handle(), started=1.0, preview="a"))
    tracker.add("c2", TurnInfo(handle=_handle(), started=2.0, preview="b"))
    assert {cid for cid, _ in tracker.all()} == {"c1", "c2"}


def test_remove_if_only_removes_matching_handle():
    """A finished turn's callback must clear only its own entry, never a newer turn's for the same
    conversation (a first turn's done-callback removing a second turn's entry)."""
    tracker = TurnTracker()
    first = TurnInfo(handle=_handle(done=True), started=1.0, preview="first")
    second = TurnInfo(handle=_handle(), started=2.0, preview="second")

    tracker.add("c1", first)
    # A second turn replaced the entry (as add() overwrites). The first turn's stale callback fires:
    tracker.add("c1", second)
    tracker.remove_if("c1", first.handle)
    assert tracker.get("c1") is second  # the newer entry survives

    # The matching handle removes its own entry.
    tracker.remove_if("c1", second.handle)
    assert tracker.get("c1") is None

    tracker.remove_if("missing", first.handle)  # no entry -> no error


def test_for_task_reports_only_the_running_firings_of_that_task():
    """Stopping a task's runs starts here: a task can have more than one firing in flight (a run-now
    alongside an armed one), and a finished entry must not be offered up for cancellation."""
    tracker = TurnTracker()
    tracker.add("c1", TurnInfo(handle=_handle(), started=1.0, preview="a", task_id="t1"))
    tracker.add("c2", TurnInfo(handle=_handle(), started=2.0, preview="b", task_id="t1"))
    tracker.add("c3", TurnInfo(handle=_handle(), started=3.0, preview="c", task_id="t2"))
    tracker.add("c4", TurnInfo(handle=_handle(done=True), started=4.0, preview="d", task_id="t1"))
    tracker.add("c5", TurnInfo(handle=_handle(), started=5.0, preview="e"))  # a reactive turn

    assert {cid for cid, _ in tracker.for_task("t1")} == {"c1", "c2"}
    assert {cid for cid, _ in tracker.for_task("t2")} == {"c3"}
    assert tracker.for_task("nope") == []


def test_live_keeps_a_turn_whose_entry_a_later_one_replaced():
    """Replacing a conversation's entry does not end the turn it replaced, so the entries are not the
    list of turns still running. Shutdown needs that list: it closes the session store, and a turn still
    running when it does dies part way through the record it makes on its way down."""
    tracker = TurnTracker()
    first = _handle()
    second = _handle()

    tracker.add("c1", TurnInfo(handle=first, started=1.0, preview="first"))
    tracker.add("c1", TurnInfo(handle=second, started=2.0, preview="second"))

    assert tracker.get("c1").handle is second
    assert tracker.live() == [first, second]


def test_live_drops_a_displaced_turn_once_it_finishes():
    """The displaced handle no longer matches its conversation's entry, so its own done-callback is the
    only thing that can clear it. If it did not, shutdown would wait on a turn that already ended."""
    tracker = TurnTracker()
    first = _handle()
    second = _handle()
    tracker.add("c1", TurnInfo(handle=first, started=1.0, preview="first"))
    tracker.add("c1", TurnInfo(handle=second, started=2.0, preview="second"))

    tracker.remove_if("c1", first)

    assert tracker.live() == [second]
    assert tracker.get("c1").handle is second, "clearing a displaced turn must not evict the live entry"


def test_live_omits_a_turn_that_has_already_finished():
    tracker = TurnTracker()
    tracker.add("c1", TurnInfo(handle=_handle(done=True), started=1.0, preview="done"))

    assert tracker.live() == []
