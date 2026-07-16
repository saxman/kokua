from datetime import datetime

import pytest

from kokua import paths, scheduling
from kokua.config import AssistantConfig


def test_scheduled_tasks_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path)
    assert cfg.scheduled_tasks_path == tmp_path / "scheduled_tasks.json"


def test_paths_scheduled_tasks_path_under_state(monkeypatch, tmp_path):
    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    assert paths.scheduled_tasks_path() == tmp_path / "data" / "scheduled_tasks.json"


def _record(task_id="abc", name="t1"):
    return {
        "id": task_id,
        "name": name,
        "prompt": "hi",
        "schedule": {"type": "interval", "seconds": 60},
        "new_session": False,
        "created_at": "2026-07-15T00:00:00",
        "enabled": True,
    }


def test_registry_add_load_roundtrip(tmp_path):
    path = tmp_path / "scheduled_tasks.json"
    scheduling.add(path, _record())
    assert scheduling.load(path) == [_record()]


def test_registry_add_replaces_same_id(tmp_path):
    path = tmp_path / "scheduled_tasks.json"
    scheduling.add(path, _record(name="first"))
    scheduling.add(path, _record(name="second"))
    records = scheduling.load(path)
    assert len(records) == 1 and records[0]["name"] == "second"


def test_registry_remove(tmp_path):
    path = tmp_path / "scheduled_tasks.json"
    scheduling.add(path, _record())
    assert scheduling.remove(path, "abc") is True
    assert scheduling.remove(path, "abc") is False
    assert scheduling.load(path) == []


def test_registry_load_tolerates_missing_and_corrupt(tmp_path):
    path = tmp_path / "scheduled_tasks.json"
    assert scheduling.load(path) == []
    path.write_text("{ not json", encoding="utf-8")
    assert scheduling.load(path) == []


def test_find_matches_id_then_name(tmp_path):
    records = [_record(task_id="id1", name="morning")]
    assert scheduling.find(records, "id1")["name"] == "morning"
    assert scheduling.find(records, "morning")["id"] == "id1"
    assert scheduling.find(records, "nope") is None


def test_next_fire_once_future_and_past():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert scheduling.next_fire({"type": "once", "at": "2026-07-15T12:00:30"}, now) == 30.0
    assert scheduling.next_fire({"type": "once", "at": "2026-07-15T11:59:30"}, now) is None


def test_next_fire_interval():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert scheduling.next_fire({"type": "interval", "seconds": 90}, now) == 90.0


def test_next_fire_daily_rolls_to_tomorrow():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert scheduling.next_fire({"type": "daily", "at": "13:00"}, now) == 3600.0
    assert scheduling.next_fire({"type": "daily", "at": "11:00"}, now) == 23 * 3600.0


def test_next_fire_weekly_rolls_within_and_across_week():
    now = datetime(2026, 7, 15, 12, 0, 0)  # a Wednesday (weekday()==2)
    assert scheduling.next_fire({"type": "weekly", "day": "thu", "at": "12:00"}, now) == 24 * 3600.0
    assert scheduling.next_fire({"type": "weekly", "day": "wed", "at": "11:00"}, now) == 7 * 24 * 3600.0


@pytest.mark.parametrize(
    "schedule",
    [
        {"type": "interval", "seconds": 0},
        {"type": "daily", "at": "25:00"},
        {"type": "daily", "at": "oops"},
        {"type": "weekly", "day": "funday", "at": "09:00"},
        {"type": "once", "at": "not-a-date"},
        {"type": "once"},
        {"type": "bogus"},
    ],
)
def test_next_fire_rejects_malformed(schedule):
    with pytest.raises(ValueError):
        scheduling.next_fire(schedule, datetime(2026, 7, 15, 12, 0, 0))
