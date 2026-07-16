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


class FakeScheduler:
    """Records at/cancel calls; a test can invoke a captured job to simulate a fire."""

    def __init__(self):
        self.jobs: dict[str, tuple[float, object]] = {}

    def at(self, delay, job, *, name):
        self.jobs[name] = (delay, job)
        return name

    def cancel(self, name):
        return self.jobs.pop(name, None) is not None


async def _noop_fire(prompt, *, new_session=False, task_name=None):
    _noop_fire.calls.append((prompt, new_session, task_name))


_noop_fire.calls = []


def _make(tmp_path, fire=_noop_fire):
    scheduler = FakeScheduler()
    path = tmp_path / "scheduled_tasks.json"
    tools, arm_all = scheduling.make_scheduler_tools(scheduler, path, fire)
    by_name = {t.__name__: t for t in tools}
    return scheduler, path, by_name, arm_all


async def test_schedule_task_persists_and_arms(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    out = await tools["schedule_task"]("do it", "daily", time_of_day="09:00", name="brief")
    records = scheduling.load(path)
    assert len(records) == 1 and records[0]["name"] == "brief" and records[0]["new_session"] is False
    assert records[0]["schedule"] == {"type": "daily", "at": "09:00"}
    assert records[0]["id"] in scheduler.jobs
    assert "brief" in out


async def test_schedule_task_flat_daily_call(tmp_path):
    # The exact shape the model failed to produce before: a flat "daily" + time, no nested dict.
    scheduler, path, tools, _ = _make(tmp_path)
    out = await tools["schedule_task"]("summarize the day", "daily", time_of_day="20:00")
    records = scheduling.load(path)
    assert len(records) == 1 and records[0]["schedule"] == {"type": "daily", "at": "20:00"}
    assert "Scheduled task" in out


async def test_schedule_task_weekday_is_normalized(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("x", "weekly", weekday="Monday", time_of_day="09:00", name="w")
    assert scheduling.load(path)[0]["schedule"] == {"type": "weekly", "day": "mon", "at": "09:00"}


async def test_schedule_task_rejects_unknown_type(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    out = await tools["schedule_task"]("x", "cron", time_of_day="20:00")
    assert "Invalid schedule" in out and "once, interval, daily, weekly" in out
    assert scheduling.load(path) == []


async def test_schedule_task_rejects_bad_schedule_and_dupe_name(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    bad = await tools["schedule_task"]("x", "daily", time_of_day="99:99")
    assert "Invalid schedule" in bad and scheduling.load(path) == []
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="dupe")
    again = await tools["schedule_task"]("y", "interval", interval_seconds=60, name="dupe")
    assert "already exists" in again and len(scheduling.load(path)) == 1


async def test_schedule_task_rejects_past_once(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    out = await tools["schedule_task"]("x", "once", at_datetime="2000-01-01T00:00:00")
    assert "past" in out.lower() and scheduling.load(path) == []


async def test_cancel_removes_record_and_job(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="k")
    task_id = scheduling.load(path)[0]["id"]
    out = await tools["cancel_scheduled_task"]("k")
    assert task_id not in scheduler.jobs and scheduling.load(path) == [] and "Cancelled" in out
    assert "No scheduled task" in await tools["cancel_scheduled_task"]("k")


async def test_list_scheduled_tasks(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    assert "No scheduled tasks" in await tools["list_scheduled_tasks"]()
    await tools["schedule_task"]("summarize inbox", "daily", time_of_day="09:00", name="brief")
    listing = await tools["list_scheduled_tasks"]()
    assert "brief" in listing and "summarize inbox" in listing


async def test_fire_job_recurring_rearms(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="r", new_session=True)
    task_id = scheduling.load(path)[0]["id"]
    _delay, job = scheduler.jobs[task_id]
    await job()  # simulate the scheduler firing
    assert _noop_fire.calls == [("ping", True, "r")]
    assert task_id in scheduler.jobs  # re-armed
    assert scheduling.load(path)  # still present


async def test_fire_job_once_removes(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    future = "2999-01-01T00:00:00"
    await tools["schedule_task"]("later", "once", at_datetime=future, name="o")
    task_id = scheduling.load(path)[0]["id"]
    _delay, job = scheduler.jobs[task_id]
    await job()
    assert scheduling.load(path) == []  # one-shot dropped after firing


async def test_fire_job_skips_rearm_if_cancelled_during_run(tmp_path):
    async def cancelling_fire(prompt, *, new_session=False, task_name=None):
        scheduling.remove(path, scheduling.load(path)[0]["id"])  # user cancelled mid-run

    scheduler, path, tools, _ = _make(tmp_path, fire=cancelling_fire)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="c")
    task_id = scheduling.load(path)[0]["id"]
    _delay, job = scheduler.jobs[task_id]
    await job()
    assert scheduling.load(path) == []  # not re-added


async def test_arm_all_arms_and_drops_past_once(tmp_path):
    scheduler, path, tools, arm_all = _make(tmp_path)
    scheduling.add(
        path,
        {
            "id": "keep",
            "name": "r",
            "prompt": "p",
            "schedule": {"type": "interval", "seconds": 60},
            "new_session": False,
            "created_at": "x",
            "enabled": True,
        },
    )
    scheduling.add(
        path,
        {
            "id": "stale",
            "name": "o",
            "prompt": "p",
            "schedule": {"type": "once", "at": "2000-01-01T00:00:00"},
            "new_session": False,
            "created_at": "x",
            "enabled": True,
        },
    )
    arm_all()
    assert "keep" in scheduler.jobs and "stale" not in scheduler.jobs
    ids = {r["id"] for r in scheduling.load(path)}
    assert ids == {"keep"}  # stale past-due one-shot dropped from the registry
