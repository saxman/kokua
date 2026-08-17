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


async def _noop_fire(prompt, *, target="active", task_name=None, session_id=None, task_id=None):
    _noop_fire.calls.append((prompt, target, task_name, session_id))
    _noop_fire.task_ids.append(task_id)
    return _noop_fire.return_key


_noop_fire.calls = []
_noop_fire.task_ids = []
_noop_fire.return_key = None

EVERY_MINUTE = {"type": "interval", "seconds": 60}
DAILY_NINE = {"type": "daily", "at": "09:00"}


def _make(tmp_path, fire=_noop_fire):
    scheduler = FakeScheduler()
    path = tmp_path / "scheduled_tasks.json"
    return scheduler, path, TaskService(scheduler, path, fire)


def test_create_persists_and_arms(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    record, delay = tasks.create("do it", DAILY_NINE, name="brief")

    stored = scheduling.load(path)
    assert len(stored) == 1 and stored[0]["name"] == "brief" and stored[0]["target"] == "active"
    assert stored[0]["schedule"] == DAILY_NINE
    assert record["id"] in scheduler.jobs and delay > 0


def test_create_with_task_target_leaves_the_session_id_for_the_first_firing(tmp_path):
    scheduler, path, tasks = _make(tmp_path)

    tasks.create("digest", DAILY_NINE, name="d", target="task")

    record = scheduling.load(path)[0]
    assert record["target"] == "task" and record["session_id"] == ""


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
    record, _ = tasks.create("summarize inbox", DAILY_NINE, name="brief", target="task")

    (item,) = tasks.list()

    assert item["id"] == record["id"] and item["name"] == "brief"
    assert item["prompt"] == "summarize inbox" and item["schedule"] == DAILY_NINE
    assert item["target"] == "task" and item["enabled"] is True
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
    record, _ = tasks.create("ping", EVERY_MINUTE, name="r", target="new")

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert _noop_fire.task_ids == [record["id"]]


async def test_fire_rearms_a_recurring_task(tmp_path):
    _noop_fire.calls = []
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("ping", EVERY_MINUTE, name="r", target="new")

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert _noop_fire.calls == [("ping", "new", "r", None)]
    assert record["id"] in scheduler.jobs and scheduling.load(path)


async def test_fire_drops_a_one_shot(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("later", {"type": "once", "at": "2999-01-01T00:00:00"}, name="o")

    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert scheduling.load(path) == []


async def test_fire_with_task_target_persists_and_reuses_the_returned_session_id(tmp_path):
    _noop_fire.calls = []
    _noop_fire.return_key = "sess-123"
    try:
        scheduler, path, tasks = _make(tmp_path)
        record, _ = tasks.create("digest", EVERY_MINUTE, name="d", target="task")
        _delay, job = scheduler.jobs[record["id"]]

        await job()  # first firing: fire() returns the newly-created conversation key
        assert _noop_fire.calls == [("digest", "task", "d", None)]
        assert scheduling.load(path)[0]["session_id"] == "sess-123"

        await job()  # second firing: the remembered key is passed in for reuse
        assert _noop_fire.calls[-1] == ("digest", "task", "d", "sess-123")
    finally:
        _noop_fire.return_key = None


async def test_fire_with_new_target_never_remembers_a_session_id(tmp_path):
    _noop_fire.calls = []
    _noop_fire.return_key = "ephemeral"
    try:
        scheduler, path, tasks = _make(tmp_path)
        record, _ = tasks.create("ping", EVERY_MINUTE, name="n", target="new")
        _delay, job = scheduler.jobs[record["id"]]
        await job()
        assert scheduling.load(path)[0].get("session_id", "") == ""
    finally:
        _noop_fire.return_key = None


async def test_fire_with_latest_target_remembers_the_key_it_will_replace(tmp_path):
    """ "latest" needs the same write-back "task" does, for the opposite reason: not to reuse the
    conversation next time but to know which one to delete."""
    _noop_fire.calls = []
    _noop_fire.return_key = "run-1"
    try:
        scheduler, path, tasks = _make(tmp_path)
        record, _ = tasks.create("digest", EVERY_MINUTE, name="l", target="latest")
        _delay, job = scheduler.jobs[record["id"]]

        await job()
        assert _noop_fire.calls == [("digest", "latest", "l", None)]
        assert scheduling.load(path)[0]["session_id"] == "run-1"

        _noop_fire.return_key = "run-2"
        await job()
        assert _noop_fire.calls[-1] == ("digest", "latest", "l", "run-1")  # handed the one to replace
        assert scheduling.load(path)[0]["session_id"] == "run-2"
    finally:
        _noop_fire.return_key = None


async def test_fire_skips_the_session_writeback_if_cancelled_during_the_run(tmp_path):
    _noop_fire.return_key = "sess-late"

    async def cancelling_fire(prompt, *, target="active", task_name=None, session_id=None, task_id=None):
        scheduling.remove(path, scheduling.load(path)[0]["id"])  # user cancelled mid-run
        return _noop_fire.return_key

    try:
        scheduler, path, tasks = _make(tmp_path, fire=cancelling_fire)
        record, _ = tasks.create("digest", EVERY_MINUTE, name="c", target="task")
        _delay, job = scheduler.jobs[record["id"]]
        await job()
        assert scheduling.load(path) == []  # not resurrected by the write-back
    finally:
        _noop_fire.return_key = None


async def test_fire_skips_the_rearm_if_cancelled_during_the_run(tmp_path):
    async def cancelling_fire(prompt, *, target="active", task_name=None, session_id=None, task_id=None):
        scheduling.remove(path, scheduling.load(path)[0]["id"])

    scheduler, path, tasks = _make(tmp_path, fire=cancelling_fire)
    record, _ = tasks.create("x", EVERY_MINUTE, name="c")
    _delay, job = scheduler.jobs[record["id"]]
    await job()

    assert scheduling.load(path) == []  # not re-added


async def test_fire_skips_the_rearm_if_disabled_during_the_run(tmp_path):
    async def disabling_fire(prompt, *, target="active", task_name=None, session_id=None, task_id=None):
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

    assert _noop_fire.calls == [("ping", "active", "r", None)]
    assert scheduler.at_count[record["id"]] == 1  # real job armed once
    assert scheduling.load(path) == before


async def test_run_now_keeps_a_one_shot(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("later", {"type": "once", "at": "2999-01-01T00:00:00"}, name="o")

    tasks.run_now("o")
    _delay, job = scheduler.jobs[f"run-now:{record['id']}"]
    await job()

    assert scheduling.load(path)  # not dropped by a manual run


async def test_run_now_with_task_target_persists_and_reuses_the_session_id(tmp_path):
    _noop_fire.calls = []
    _noop_fire.return_key = "sess-run"
    try:
        scheduler, path, tasks = _make(tmp_path)
        record, _ = tasks.create("digest", EVERY_MINUTE, name="d", target="task")

        tasks.run_now("d")
        _delay, job = scheduler.jobs[f"run-now:{record['id']}"]
        await job()
        assert _noop_fire.calls[-1] == ("digest", "task", "d", None)
        assert scheduling.load(path)[0]["session_id"] == "sess-run"

        tasks.run_now("d")
        _delay, job = scheduler.jobs[f"run-now:{record['id']}"]
        await job()
        assert _noop_fire.calls[-1] == ("digest", "task", "d", "sess-run")
    finally:
        _noop_fire.return_key = None


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
    before, _ = tasks.create("old", DAILY_NINE, name="news", target="task")

    record, changed = tasks.update("news", prompt="new and longer")

    assert changed == ["prompt"]
    stored = scheduling.load(path)[0]
    assert stored["prompt"] == "new and longer"
    assert stored["id"] == before["id"] and stored["created_at"] == before["created_at"]
    assert stored["schedule"] == DAILY_NINE and stored["target"] == "task"


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


def test_update_rejects_an_unknown_target(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", EVERY_MINUTE, name="t")

    with pytest.raises(scheduling.InvalidTarget):
        tasks.update("t", target="elsewhere")


def test_update_never_arms_a_disabled_task(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    record, _ = tasks.create("p", DAILY_NINE, name="d")
    tasks.set_enabled("d", False)

    tasks.update("d", schedule={"type": "daily", "at": "10:00"})

    assert record["id"] not in scheduler.jobs
    assert scheduling.load(path)[0]["schedule"] == {"type": "daily", "at": "10:00"}


def test_update_of_the_target_keeps_the_dedicated_conversation(tmp_path):
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", EVERY_MINUTE, name="t", target="task")
    record = scheduling.load(path)[0]
    record["session_id"] = "sess-1"
    scheduling.add(path, record)

    tasks.update("t", target="new")

    stored = scheduling.load(path)[0]
    assert stored["target"] == "new"
    assert stored["session_id"] == "sess-1"  # reused if the target flips back


def test_update_onto_the_latest_target_forgets_the_conversation_it_would_replace(tmp_path):
    """Moving a task onto "latest" must not make its next firing delete the history the task built up
    under "task". The remembered key is what "latest" replaces, so switching clears it and the first
    firing on the new target replaces nothing."""
    scheduler, path, tasks = _make(tmp_path)
    tasks.create("p", EVERY_MINUTE, name="t", target="task")
    record = scheduling.load(path)[0]
    record["session_id"] = "long-history"
    scheduling.add(path, record)

    tasks.update("t", target="latest")

    stored = scheduling.load(path)[0]
    assert stored["target"] == "latest"
    assert stored["session_id"] == ""


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
