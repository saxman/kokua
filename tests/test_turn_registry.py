"""Unit tests for TurnTracker: per-conversation in-flight turn bookkeeping."""

from __future__ import annotations

from types import SimpleNamespace

from kokua.turn_registry import TurnInfo, TurnTracker


def _handle(done=False):
    return SimpleNamespace(done=done, cancel=lambda: True)


def test_add_get_remove():
    tracker = TurnTracker()
    info = TurnInfo(handle=_handle(), started=1.0, preview="hi")
    tracker.add("c1", info)
    assert tracker.get("c1") is info
    assert tracker.running("c1") is True
    tracker.remove("c1")
    assert tracker.get("c1") is None
    assert tracker.running("c1") is False


def test_running_false_when_handle_done():
    tracker = TurnTracker()
    tracker.add("c1", TurnInfo(handle=_handle(done=True), started=1.0, preview="x"))
    assert tracker.running("c1") is False


def test_active_ids_and_all():
    tracker = TurnTracker()
    tracker.add("c1", TurnInfo(handle=_handle(), started=1.0, preview="a"))
    tracker.add("c2", TurnInfo(handle=_handle(), started=2.0, preview="b"))
    assert set(tracker.active_ids()) == {"c1", "c2"}
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
