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
