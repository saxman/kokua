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
        self.at_count: dict[str, int] = {}

    def at(self, delay, job, *, name):
        self.jobs[name] = (delay, job)
        self.at_count[name] = self.at_count.get(name, 0) + 1
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


async def test_disable_cancels_job_and_clears_flag(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="d")
    task_id = scheduling.load(path)[0]["id"]
    out = await tools["disable_scheduled_task"]("d")
    assert task_id not in scheduler.jobs  # live job cancelled
    record = scheduling.load(path)[0]
    assert record["enabled"] is False and record["id"] == task_id  # kept in registry
    assert "Disabled" in out


async def test_disable_already_disabled_reports(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="d")
    await tools["disable_scheduled_task"]("d")
    out = await tools["disable_scheduled_task"]("d")
    assert "already disabled" in out.lower()


async def test_enable_rearms_and_sets_flag(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="e")
    task_id = scheduling.load(path)[0]["id"]
    await tools["disable_scheduled_task"]("e")
    out = await tools["enable_scheduled_task"]("e")
    assert task_id in scheduler.jobs  # re-armed
    assert scheduling.load(path)[0]["enabled"] is True
    assert "Enabled" in out


async def test_enable_already_enabled_reports(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="e")
    out = await tools["enable_scheduled_task"]("e")
    assert "already enabled" in out.lower()


async def test_enable_past_due_once_reports_wont_fire(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    # Persist a disabled, past-due one-shot directly (schedule_task rejects past times).
    scheduling.add(
        path,
        {
            "id": "past",
            "name": "p",
            "prompt": "x",
            "schedule": {"type": "once", "at": "2000-01-01T00:00:00"},
            "new_session": False,
            "created_at": "x",
            "enabled": False,
        },
    )
    out = await tools["enable_scheduled_task"]("p")
    assert "past" in out.lower() and "past" not in scheduler.jobs  # not armed
    assert scheduling.load(path)[0]["enabled"] is True  # flag still flipped


async def test_enable_disable_unknown_reports(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    assert "No scheduled task" in await tools["enable_scheduled_task"]("nope")
    assert "No scheduled task" in await tools["disable_scheduled_task"]("nope")


async def test_arm_all_skips_disabled(tmp_path):
    scheduler, path, tools, arm_all = _make(tmp_path)
    scheduling.add(
        path,
        {
            "id": "off",
            "name": "off",
            "prompt": "p",
            "schedule": {"type": "interval", "seconds": 60},
            "new_session": False,
            "created_at": "x",
            "enabled": False,
        },
    )
    arm_all()
    assert "off" not in scheduler.jobs  # not armed
    assert {r["id"] for r in scheduling.load(path)} == {"off"}  # but kept


async def test_fire_job_skips_rearm_when_disabled_during_run(tmp_path):
    async def disabling_fire(prompt, *, new_session=False, task_name=None):
        record = scheduling.load(path)[0]
        record["enabled"] = False
        scheduling.add(path, record)  # user disabled mid-run

    scheduler, path, tools, _ = _make(tmp_path, fire=disabling_fire)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="r")
    task_id = scheduling.load(path)[0]["id"]
    _delay, job = scheduler.jobs[task_id]
    await job()
    assert scheduler.at_count[task_id] == 1  # armed once at schedule time, not re-armed after firing
    assert scheduling.load(path)[0]["enabled"] is False  # kept, still disabled


async def test_list_shows_disabled_state(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("summarize inbox", "daily", time_of_day="09:00", name="brief")
    await tools["disable_scheduled_task"]("brief")
    listing = await tools["list_scheduled_tasks"]()
    assert "disabled" in listing.lower()


async def test_run_now_enqueues_job_and_fires_by_id(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="r", new_session=True)
    task_id = scheduling.load(path)[0]["id"]
    out = await tools["run_scheduled_task"](task_id)
    assert "now" in out.lower() and "r" in out and "new conversation" in out.lower()
    job_name = f"run-now:{task_id}"
    assert job_name in scheduler.jobs
    _delay, job = scheduler.jobs[job_name]
    await job()  # simulate the scheduler firing the run-now job
    assert _noop_fire.calls == [("ping", True, "r")]


async def test_run_now_by_name(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="byname")
    task_id = scheduling.load(path)[0]["id"]
    await tools["run_scheduled_task"]("byname")
    _delay, job = scheduler.jobs[f"run-now:{task_id}"]
    await job()
    assert _noop_fire.calls == [("ping", False, "byname")]


async def test_run_now_does_not_disturb_schedule(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="r")
    task_id = scheduling.load(path)[0]["id"]
    before = scheduling.load(path)
    await tools["run_scheduled_task"](task_id)
    _delay, job = scheduler.jobs[f"run-now:{task_id}"]
    await job()
    assert scheduler.at_count[task_id] == 1  # real job armed once, not re-armed by the manual run
    assert scheduling.load(path) == before  # registry unchanged


async def test_run_now_keeps_one_shot(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("later", "once", at_datetime="2999-01-01T00:00:00", name="o")
    task_id = scheduling.load(path)[0]["id"]
    await tools["run_scheduled_task"](task_id)
    _delay, job = scheduler.jobs[f"run-now:{task_id}"]
    await job()
    assert scheduling.load(path)  # one-shot NOT dropped by a manual run


async def test_run_now_allows_disabled_and_notes_it(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="d")
    await tools["disable_scheduled_task"]("d")
    task_id = scheduling.load(path)[0]["id"]
    out = await tools["run_scheduled_task"]("d")
    assert "disabled" in out.lower()
    _delay, job = scheduler.jobs[f"run-now:{task_id}"]
    await job()
    assert _noop_fire.calls == [("ping", False, "d")]


async def test_run_now_unknown_reports_and_enqueues_nothing(tmp_path):
    scheduler, path, tools, _ = _make(tmp_path)
    out = await tools["run_scheduled_task"]("nope")
    assert "No scheduled task" in out
    assert scheduler.jobs == {}


async def test_run_now_skips_fire_if_cancelled_before_firing(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tools, _ = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="c")
    task_id = scheduling.load(path)[0]["id"]
    await tools["run_scheduled_task"](task_id)
    scheduling.remove(path, task_id)  # cancelled between enqueue and fire
    _delay, job = scheduler.jobs[f"run-now:{task_id}"]
    await job()
    assert _noop_fire.calls == []


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
