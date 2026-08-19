"""The task lifecycle: registry writes paired with scheduler (un)arming, and the firing path.

These exercise ``TaskService`` directly, so they assert on records and raised errors. The sentences a
model reads are covered in ``tests/toolsets/test_scheduling.py``.
"""

import pytest

from kokua import scheduling
from kokua.scheduling import TaskService


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


async def _noop_fire(prompt, *, task_name=None, task_id=None, max_conversations=0):
    _noop_fire.calls.append((prompt, task_name, max_conversations))
    _noop_fire.task_ids.append(task_id)


_noop_fire.calls = []
_noop_fire.task_ids = []

EVERY_MINUTE = {"type": "interval", "seconds": 60}
DAILY_NINE = {"type": "daily", "at": "09:00"}


def _make(tmp_path, fire=_noop_fire, default_max=0):
    scheduler = FakeScheduler()
    path = tmp_path / "scheduled_tasks.json"
    return scheduler, path, TaskService(scheduler, path, fire, default_max_conversations=lambda: default_max)


def test_create_persists_and_arms(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    record, delay = tasks.create("do it", DAILY_NINE, name="brief")

    stored = scheduling.load(path)
    assert len(stored) == 1 and stored[0]["name"] == "brief" and stored[0]["max_conversations"] is None
    assert stored[0]["schedule"] == DAILY_NINE
    assert record["id"] in scheduler.jobs and delay > 0


def test_create_records_an_explicit_retention_cap(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    tasks.create("digest", DAILY_NINE, name="d", max_conversations=1)

    assert scheduling.load(path)[0]["max_conversations"] == 1


def test_create_rejects_a_negative_retention_cap_and_writes_nothing(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    with pytest.raises(scheduling.InvalidRetention):
        tasks.create("digest", DAILY_NINE, name="d", max_conversations=-1)
    assert scheduling.load(path) == []


def test_create_rejects_a_bad_schedule_and_writes_nothing(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    with pytest.raises(scheduling.ScheduleInvalid):
        tasks.create("x", {"type": "daily", "at": "99:99"})

    assert scheduling.load(path) == []


def test_create_rejects_a_past_one_shot(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    with pytest.raises(scheduling.SchedulePast):
        tasks.create("x", {"type": "once", "at": "2000-01-01T00:00:00"})

    assert scheduling.load(path) == []


def test_create_rejects_a_duplicate_name(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("x", EVERY_MINUTE, name="dupe")

    with pytest.raises(scheduling.DuplicateName):
        tasks.create("y", EVERY_MINUTE, name="dupe")

    assert len(scheduling.load(path)) == 1


def test_get_resolves_by_id_and_name_and_raises_otherwise(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("x", EVERY_MINUTE, name="k")

    assert tasks.get("k")["id"] == record["id"]
    assert tasks.get(record["id"])["name"] == "k"
    with pytest.raises(scheduling.TaskNotFound):
        tasks.get("nope")


def test_cancel_removes_the_record_and_the_job(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("x", EVERY_MINUTE, name="k")

    tasks.cancel("k")

    assert record["id"] not in scheduler.jobs and scheduling.load(path) == []
    with pytest.raises(scheduling.TaskNotFound):
        tasks.cancel("k")


def test_list_returns_fields_rather_than_prose(tmp_path):
    """The web sidebar needs fields; the prose listing is the toolset's job."""
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("summarize inbox", DAILY_NINE, name="brief", max_conversations=2)

    (item,) = tasks.list()

    assert item["id"] == record["id"] and item["name"] == "brief"
    assert item["prompt"] == "summarize inbox" and item["schedule"] == DAILY_NINE
    assert item["max_conversations"] == 2 and item["enabled"] is True
    assert item["status"] == "pending"


def test_list_reports_seconds_so_each_front_end_formats_its_own_countdown(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("x", {"type": "interval", "seconds": 3600}, name="live")

    assert 3500 < tasks.list()[0]["next_fire_seconds"] <= 3600

    tasks.set_enabled("live", False)
    item = tasks.list()[0]
    assert item["next_fire_seconds"] is None and item["status"] == "disabled"


def test_next_firing_distinguishes_the_three_reasons_there_is_no_countdown(tmp_path):
    """Each is a status, not a sentence: the sidebar and the model word them differently."""
    scheduler, path, tasks = _make(tmp_path)

    assert tasks.next_firing({"schedule": EVERY_MINUTE, "enabled": False}) == ("disabled", None)
    assert tasks.next_firing({"schedule": {"type": "bogus"}}) == ("invalid", None)
    assert tasks.next_firing({"schedule": {"type": "once", "at": "2000-01-01T00:00:00"}}) == ("past", None)
    status, seconds = tasks.next_firing({"schedule": EVERY_MINUTE})
    assert status == "pending" and seconds is not None


def test_set_enabled_cancels_and_rearms_the_scheduler_job(tmp_path):
    """Disabling has to drop the armed job and enabling re-arm it, or the in-memory scheduler keeps
    firing a task the registry calls disabled."""
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("x", EVERY_MINUTE, name="d")

    result = tasks.set_enabled("d", False)
    assert result.changed is True and result.armed is False
    assert record["id"] not in scheduler.jobs and scheduling.load(path)[0]["enabled"] is False

    result = tasks.set_enabled("d", True)
    assert result.changed is True and result.armed is True
    assert record["id"] in scheduler.jobs and scheduling.load(path)[0]["enabled"] is True


def test_set_enabled_reports_a_no_op(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("x", EVERY_MINUTE, name="d")

    assert tasks.set_enabled("d", True).changed is False


def test_enabling_a_past_due_one_shot_flips_the_flag_without_arming(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    scheduling.add(
        path,
        {
            "id": "past",
            "name": "p",
            "prompt": "x",
            "schedule": {"type": "once", "at": "2000-01-01T00:00:00"},
            "created_at": "x",
            "enabled": False,
        },
    )

    result = tasks.set_enabled("p", True)

    assert result.changed is True and result.armed is False
    assert "past" not in scheduler.jobs
    assert scheduling.load(path)[0]["enabled"] is True


def test_set_enabled_and_run_now_reject_an_unknown_handle(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    for call in (lambda: tasks.set_enabled("nope", False), lambda: tasks.run_now("nope")):
        with pytest.raises(scheduling.TaskNotFound):
            call()


def test_run_now_leaves_the_armed_job_alone(tmp_path):
    """Running a task by hand must not disturb when it next fires, so the run-now job is a separate
    scheduler entry rather than a re-arm of the record's own."""
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("x", EVERY_MINUTE, name="r")

    tasks.run_now("r")

    assert scheduler.at_count[record["id"]] == 1  # untouched
    assert f"run-now:{record['id']}" in scheduler.jobs


# -- firing ----------------------------------------------------------------------------------------


async def test_fire_passes_the_task_id_so_its_conversation_can_be_grouped(tmp_path):
    _noop_fire.calls, _noop_fire.task_ids = [], []
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("ping", EVERY_MINUTE, name="r")

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert _noop_fire.task_ids == [record["id"]]


async def test_fire_rearms_a_recurring_task(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("ping", EVERY_MINUTE, name="r")

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert _noop_fire.calls == [("ping", "r", 0)]
    assert record["id"] in scheduler.jobs and scheduling.load(path)


async def test_fire_drops_a_one_shot(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("later", {"type": "once", "at": "2999-01-01T00:00:00"}, name="o")

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert scheduling.load(path) == []


async def test_fire_passes_the_records_own_retention_cap(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path, default_max=3)
    record, _ = tasks.create("digest", EVERY_MINUTE, name="d", max_conversations=1)

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert _noop_fire.calls == [("digest", "d", 1)]


async def test_fire_falls_back_to_the_configured_default_cap(tmp_path):
    """A record with no cap of its own inherits the default, read at fire time so a settings change
    reaches the next firing without a restart."""
    _noop_fire.calls = []
    default = [3]
    scheduler = FakeScheduler()
    path = tmp_path / "scheduled_tasks.json"
    tasks = TaskService(scheduler, path, _noop_fire, default_max_conversations=lambda: default[0])
    record, _ = tasks.create("digest", EVERY_MINUTE, name="d")

    _delay, job = scheduler.jobs[record["id"]]
    await job()
    assert _noop_fire.calls[-1] == ("digest", "d", 3)

    default[0] = 5
    await job()
    assert _noop_fire.calls[-1] == ("digest", "d", 5)


async def test_fire_reads_an_explicit_zero_as_unlimited_rather_than_as_unset(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path, default_max=3)
    record, _ = tasks.create("digest", EVERY_MINUTE, name="d", max_conversations=0)

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert _noop_fire.calls == [("digest", "d", 0)]


async def test_fire_writes_nothing_back_to_the_registry(tmp_path):
    """Nothing reuses a conversation any more, so a firing has no reason to touch the record. A
    write-back is what used to risk resurrecting a task the user cancelled mid-run."""
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("digest", EVERY_MINUTE, name="d")
    before = path.read_text(encoding="utf-8")

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert path.read_text(encoding="utf-8") == before


async def test_fire_ignores_the_target_and_session_id_of_a_pre_retention_record(tmp_path):
    """Registry records written before retention still carry a ``target`` and a ``session_id``. Both
    are dead fields: the firing mints its own conversation and inherits the default cap."""
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path, default_max=3)
    record, _ = tasks.create("digest", EVERY_MINUTE, name="d")
    stored = scheduling.load(path)[0]
    stored.update({"target": "task", "session_id": "sess-legacy"})
    stored.pop("max_conversations")
    scheduling.add(path, stored)

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert _noop_fire.calls == [("digest", "d", 3)]


async def test_fire_skips_the_rearm_if_cancelled_during_the_run(tmp_path):
    async def cancelling_fire(prompt, *, task_name=None, task_id=None, max_conversations=0):
        scheduling.remove(path, scheduling.load(path)[0]["id"])

    scheduler, path, tasks = _make(tmp_path, fire=cancelling_fire)
    record, _ = tasks.create("x", EVERY_MINUTE, name="c")
    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert scheduling.load(path) == []  # not re-added


async def test_fire_skips_the_rearm_if_disabled_during_the_run(tmp_path):
    async def disabling_fire(prompt, *, task_name=None, task_id=None, max_conversations=0):
        record = scheduling.load(path)[0]
        record["enabled"] = False
        scheduling.add(path, record)  # user disabled mid-run

    scheduler, path, tasks = _make(tmp_path, fire=disabling_fire)
    record, _ = tasks.create("x", EVERY_MINUTE, name="r")
    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert scheduler.at_count[record["id"]] == 1  # armed at create time, not re-armed after firing
    assert scheduling.load(path)[0]["enabled"] is False


async def test_run_now_fires_without_disturbing_the_schedule(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("ping", EVERY_MINUTE, name="r")
    before = scheduling.load(path)

    tasks.run_now("r")
    _delay, job = scheduler.jobs[f"run-now:{record['id']}"]
    await job()

    assert _noop_fire.calls == [("ping", "r", 0)]
    assert scheduler.at_count[record["id"]] == 1  # real job armed once
    assert scheduling.load(path) == before


async def test_run_now_keeps_a_one_shot(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("later", {"type": "once", "at": "2999-01-01T00:00:00"}, name="o")

    tasks.run_now("o")
    _delay, job = scheduler.jobs[f"run-now:{record['id']}"]
    await job()

    assert scheduling.load(path)  # not dropped by a manual run


async def test_run_now_fires_with_the_same_retention_cap_a_schedule_would(tmp_path):
    """A manual run reproduces exactly what the next scheduled firing would do, pruning included."""
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path, default_max=3)
    record, _ = tasks.create("digest", EVERY_MINUTE, name="d", max_conversations=2)

    tasks.run_now("d")
    _delay, job = scheduler.jobs[f"run-now:{record['id']}"]
    await job()

    assert _noop_fire.calls[-1] == ("digest", "d", 2)


async def test_run_now_skips_the_firing_if_cancelled_before_it_runs(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("ping", EVERY_MINUTE, name="c")

    tasks.run_now("c")
    scheduling.remove(path, record["id"])  # cancelled between enqueue and fire
    _delay, job = scheduler.jobs[f"run-now:{record['id']}"]
    await job()

    assert _noop_fire.calls == []


# -- boot arming -----------------------------------------------------------------------------------


def _persisted(task_id, schedule, *, enabled=True):
    return {
        "id": task_id,
        "name": task_id,
        "prompt": "p",
        "schedule": schedule,
        "created_at": "x",
        "enabled": enabled,
    }


def test_arm_all_skips_a_disabled_task_but_keeps_it(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    scheduling.add(path, _persisted("off", EVERY_MINUTE, enabled=False))

    tasks.arm_all()

    assert "off" not in scheduler.jobs
    assert {r["id"] for r in scheduling.load(path)} == {"off"}


def test_arm_all_arms_the_live_and_drops_a_past_due_one_shot(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    scheduling.add(path, _persisted("keep", EVERY_MINUTE))
    scheduling.add(path, _persisted("stale", {"type": "once", "at": "2000-01-01T00:00:00"}))

    tasks.arm_all()

    assert "keep" in scheduler.jobs and "stale" not in scheduler.jobs
    assert {r["id"] for r in scheduling.load(path)} == {"keep"}


# -- editing ---------------------------------------------------------------------------------------


def test_update_keeps_identity_and_untouched_fields(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    before, _ = tasks.create("old", DAILY_NINE, name="news", max_conversations=2)

    record, changed = tasks.update("news", prompt="new and longer")

    assert changed == ["prompt"]
    stored = scheduling.load(path)[0]
    assert stored["prompt"] == "new and longer"
    assert stored["id"] == before["id"] and stored["created_at"] == before["created_at"]
    assert stored["schedule"] == DAILY_NINE and stored["max_conversations"] == 2


def test_update_of_a_prompt_does_not_restart_the_countdown(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("old", EVERY_MINUTE, name="r")

    tasks.update("r", prompt="new")

    assert scheduler.at_count[record["id"]] == 1


def test_update_of_the_schedule_rearms(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("p", DAILY_NINE, name="d")

    _record, changed = tasks.update("d", schedule={"type": "daily", "at": "10:30"})

    assert changed == ["schedule"]
    assert scheduler.at_count[record["id"]] == 2


def test_update_rejects_an_invalid_schedule_and_applies_nothing_else(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", DAILY_NINE, name="d")
    before = scheduling.load(path)

    with pytest.raises(scheduling.ScheduleInvalid):
        tasks.update("d", prompt="edited", schedule={"type": "daily", "at": "99:99"})

    assert scheduling.load(path) == before


def test_update_rejects_a_past_one_shot(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", {"type": "once", "at": "2999-01-01T00:00:00"}, name="o")
    before = scheduling.load(path)

    with pytest.raises(scheduling.SchedulePast):
        tasks.update("o", schedule={"type": "once", "at": "2000-01-01T00:00:00"})

    assert scheduling.load(path) == before


def test_update_rejects_a_duplicate_name_but_allows_the_current_one(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("a", EVERY_MINUTE, name="one")
    tasks.create("b", EVERY_MINUTE, name="two")

    with pytest.raises(scheduling.DuplicateName):
        tasks.update("two", name="one")
    assert {r["name"] for r in scheduling.load(path)} == {"one", "two"}

    _record, changed = tasks.update("two", name="two", prompt="c")
    assert changed == ["prompt"]


def test_update_rejects_a_negative_retention_cap(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", EVERY_MINUTE, name="t")

    with pytest.raises(scheduling.InvalidRetention):
        tasks.update("t", max_conversations=-2)


def test_update_never_arms_a_disabled_task(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("p", DAILY_NINE, name="d")
    tasks.set_enabled("d", False)

    tasks.update("d", schedule={"type": "daily", "at": "10:00"})

    assert record["id"] not in scheduler.jobs
    assert scheduling.load(path)[0]["schedule"] == {"type": "daily", "at": "10:00"}


def test_update_of_the_retention_cap_is_a_reported_change(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", EVERY_MINUTE, name="t", max_conversations=3)

    _record, changed = tasks.update("t", max_conversations=1)

    assert changed == ["max_conversations"]
    assert scheduling.load(path)[0]["max_conversations"] == 1


def test_update_can_set_an_unlimited_cap(tmp_path):
    """Zero is a real value, not "leave it alone": a task can be moved off a cap it was created with."""
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", EVERY_MINUTE, name="t", max_conversations=1)

    _record, changed = tasks.update("t", max_conversations=0)

    assert changed == ["max_conversations"]
    assert scheduling.load(path)[0]["max_conversations"] == 0


def test_update_with_nothing_to_change_is_a_no_op(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", EVERY_MINUTE, name="r")
    before = scheduling.load(path)

    _record, changed = tasks.update("r")

    assert changed == [] and scheduling.load(path) == before


def test_update_of_an_unknown_task_raises(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    with pytest.raises(scheduling.TaskNotFound):
        tasks.update("nope", prompt="x")


# -- stopping a run --------------------------------------------------------------------------------


def test_stop_cancels_the_runs_in_flight_and_leaves_the_task_armed(tmp_path):
    """Stopping a run is not disabling a task. Which is why the cancelling is injected rather than done
    with ``scheduler.cancel``: that would reach the running job, but it unregisters it too, so a stop
    would silently disarm the schedule."""
    asked: list[str] = []

    def stop_run(task_id):
        asked.append(task_id)
        return 2, False

    scheduler, path, tasks = _make(tmp_path)
    tasks = TaskService(scheduler, path, _noop_fire, stop_run=stop_run)
    record, _ = tasks.create("do it", EVERY_MINUTE, name="brief")

    result = tasks.stop("brief")

    assert asked == [record["id"]]
    assert result.stopped == 2 and result.skipped_self is False
    assert record["id"] in scheduler.jobs  # still armed for its next firing
    assert scheduling.load(path)[0]["enabled"] is True


def test_stop_reports_nothing_running_without_touching_the_registry(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("do it", EVERY_MINUTE, name="brief")

    result = tasks.stop("brief")

    assert result.stopped == 0 and result.skipped_self is False


def test_stop_raises_for_an_unknown_handle(tmp_path):
    from kokua.scheduling.tasks import TaskNotFound

    scheduler, path, tasks = _make(tmp_path)

    with pytest.raises(TaskNotFound):
        tasks.stop("nope")
