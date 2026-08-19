"""The ``scheduling`` toolset: the flat schedule arguments a model fills, and the sentences it reads.

The lifecycle itself is covered in ``tests/scheduling/test_tasks.py``; these assert on the tool
surface, which is the half that exists for the model.
"""

from kokua import scheduling
from kokua.scheduling import TaskService
from kokua.toolsets.scheduling import make_scheduling_tools
from tests.scheduling.test_tasks import FakeScheduler, _noop_fire


def _make(tmp_path):
    scheduler = FakeScheduler()
    path = tmp_path / "scheduled_tasks.json"
    tasks = TaskService(scheduler, path, _noop_fire, default_max_conversations=lambda: 3)
    return scheduler, path, {fn.__name__: fn for fn in make_scheduling_tools(tasks)}


# -- schedule_task ---------------------------------------------------------------------------------


async def test_schedule_task_takes_a_flat_daily_call(tmp_path):
    # The exact shape the model failed to produce before: a flat "daily" + time, no nested dict.
    scheduler, path, tools = _make(tmp_path)

    out = await tools["schedule_task"]("summarize the day", "daily", time_of_day="20:00")

    assert scheduling.load(path)[0]["schedule"] == {"type": "daily", "at": "20:00"}
    assert "Scheduled task" in out and "first run in ~" in out


async def test_schedule_task_normalizes_a_weekday(tmp_path):
    scheduler, path, tools = _make(tmp_path)

    await tools["schedule_task"]("x", "weekly", weekday="Monday", time_of_day="09:00", name="w")

    assert scheduling.load(path)[0]["schedule"] == {"type": "weekly", "day": "mon", "at": "09:00"}


async def test_schedule_task_names_the_valid_types_when_given_another(tmp_path):
    scheduler, path, tools = _make(tmp_path)

    out = await tools["schedule_task"]("x", "cron", time_of_day="20:00")

    assert "Invalid schedule" in out and "once, interval, daily, weekly" in out
    assert scheduling.load(path) == []


async def test_schedule_task_reports_a_bad_time_a_past_time_and_a_taken_name(tmp_path):
    scheduler, path, tools = _make(tmp_path)

    assert "Invalid schedule" in await tools["schedule_task"]("x", "daily", time_of_day="99:99")
    assert "past" in (await tools["schedule_task"]("x", "once", at_datetime="2000-01-01T00:00:00")).lower()
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="dupe")
    assert "already exists" in await tools["schedule_task"]("y", "interval", interval_seconds=60, name="dupe")
    assert len(scheduling.load(path)) == 1


# -- reading ---------------------------------------------------------------------------------------


async def test_list_scheduled_tasks(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    assert "No scheduled tasks" in await tools["list_scheduled_tasks"]()

    await tools["schedule_task"]("summarize inbox", "daily", time_of_day="09:00", name="brief", max_conversations=1)

    listing = await tools["list_scheduled_tasks"]()
    assert "brief" in listing and "summarize inbox" in listing and "keep=1" in listing


async def test_list_shows_a_disabled_task_as_disabled(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("summarize inbox", "daily", time_of_day="09:00", name="brief")
    await tools["disable_scheduled_task"]("brief")

    assert "disabled" in (await tools["list_scheduled_tasks"]()).lower()


LONG_PROMPT = "Search the web for at least five verified AI news articles, then summarize each in two sentences."


async def test_list_marks_a_truncated_prompt(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"](LONG_PROMPT, "daily", time_of_day="09:00", name="news")

    listing = await tools["list_scheduled_tasks"]()

    assert LONG_PROMPT not in listing  # still a preview
    assert "..." in listing and "get_scheduled_task" in listing


async def test_get_scheduled_task_returns_the_whole_prompt(tmp_path):
    # Editing a task requires reading its current prompt; the listing's preview is not enough.
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"](LONG_PROMPT, "daily", time_of_day="09:00", name="news", max_conversations=2)

    out = await tools["get_scheduled_task"]("news")

    assert LONG_PROMPT in out
    assert "news" in out and "daily" in out and "keep: 2" in out


# -- editing ---------------------------------------------------------------------------------------


async def test_update_merges_omitted_schedule_fields_from_the_record(tmp_path):
    """The flat arguments are the tool's, so the merge is too: a weekly task keeps its day."""
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("p", "weekly", weekday="mon", time_of_day="09:00", name="w")

    out = await tools["update_scheduled_task"]("w", time_of_day="10:30")

    assert scheduling.load(path)[0]["schedule"] == {"type": "weekly", "day": "mon", "at": "10:30"}
    assert "Updated" in out and "schedule" in out


async def test_update_can_change_the_schedule_type_and_keeps_the_rest(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("p", "daily", time_of_day="09:00", name="d")

    await tools["update_scheduled_task"]("d", schedule_type="weekly", weekday="Friday")

    assert scheduling.load(path)[0]["schedule"] == {"type": "weekly", "day": "fri", "at": "09:00"}


async def test_update_reports_a_disabled_task_stays_disabled(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("p", "daily", time_of_day="09:00", name="d")
    await tools["disable_scheduled_task"]("d")

    out = await tools["update_scheduled_task"]("d", time_of_day="10:00")

    assert "disabled" in out.lower()


async def test_update_renders_each_rejection(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("p", "daily", time_of_day="09:00", name="d")
    await tools["schedule_task"]("q", "interval", interval_seconds=60, name="other")
    before = scheduling.load(path)

    assert "Invalid schedule" in await tools["update_scheduled_task"]("d", prompt="edited", time_of_day="99:99")
    assert "already exists" in await tools["update_scheduled_task"]("other", name="d")
    assert "No scheduled task" in await tools["update_scheduled_task"]("nope", prompt="x")
    assert scheduling.load(path) == before  # nothing applied by any of them


async def test_update_with_no_fields_says_so(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("p", "interval", interval_seconds=60, name="r")

    assert "Nothing to update" in await tools["update_scheduled_task"]("r")


# -- lifecycle -------------------------------------------------------------------------------------


async def test_cancel_scheduled_task(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="k")

    assert "Cancelled" in await tools["cancel_scheduled_task"]("k")
    assert "No scheduled task" in await tools["cancel_scheduled_task"]("k")


async def test_disable_and_enable_report_the_transition_and_the_no_op(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("x", "interval", interval_seconds=60, name="d")

    assert "Disabled" in await tools["disable_scheduled_task"]("d")
    assert "already disabled" in (await tools["disable_scheduled_task"]("d")).lower()
    assert "Enabled" in await tools["enable_scheduled_task"]("d")
    assert "already enabled" in (await tools["enable_scheduled_task"]("d")).lower()


async def test_enable_warns_that_a_past_due_one_shot_will_not_fire(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    # Persisted directly: schedule_task rejects a past time.
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

    out = await tools["enable_scheduled_task"]("p")

    assert "past" in out.lower() and "will not fire" in out


async def test_enable_and_disable_report_an_unknown_handle(tmp_path):
    scheduler, path, tools = _make(tmp_path)

    assert "No scheduled task" in await tools["enable_scheduled_task"]("nope")
    assert "No scheduled task" in await tools["disable_scheduled_task"]("nope")


async def test_run_scheduled_task_says_where_the_output_will_appear(tmp_path):
    """Every firing runs in its own conversation, so the model has to be told the output is not
    coming back as this tool's return value."""
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="r")

    out = await tools["run_scheduled_task"]("r")

    assert "now" in out.lower() and "r" in out and "new conversation" in out.lower()


async def test_schedule_task_reports_a_negative_retention_cap(tmp_path):
    scheduler, path, tools = _make(tmp_path)

    out = await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="t", max_conversations=-1)

    assert "-1" in out and "0" in out and scheduling.load(path) == []


async def test_get_scheduled_task_marks_an_inherited_cap_as_the_default(tmp_path):
    """A task that never chose a cap follows the configured default, and the model has to be able to
    tell that apart from one pinned to the same number."""
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="t")

    out = await tools["get_scheduled_task"]("t")

    assert "keep: 3 (default)" in out


async def test_run_scheduled_task_notes_a_disabled_task_and_still_runs_it(tmp_path):
    scheduler, path, tools = _make(tmp_path)
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="d")
    await tools["disable_scheduled_task"]("d")
    task_id = scheduling.load(path)[0]["id"]

    out = await tools["run_scheduled_task"]("d")

    assert "disabled" in out.lower()
    assert f"run-now:{task_id}" in scheduler.jobs


async def test_run_scheduled_task_reports_an_unknown_handle_and_enqueues_nothing(tmp_path):
    scheduler, path, tools = _make(tmp_path)

    assert "No scheduled task" in await tools["run_scheduled_task"]("nope")
    assert scheduler.jobs == {}


# -- stop_scheduled_task ---------------------------------------------------------------------------


def _make_with_stop(tmp_path, stop_run):
    scheduler = FakeScheduler()
    path = tmp_path / "scheduled_tasks.json"
    tasks = TaskService(scheduler, path, _noop_fire, default_max_conversations=lambda: 3, stop_run=stop_run)
    return scheduler, path, {fn.__name__: fn for fn in make_scheduling_tools(tasks)}


async def test_stop_scheduled_task_says_what_it_stopped_and_that_the_schedule_stands(tmp_path):
    """The model has to be able to tell the user the task will be back, or it reads as a cancel."""
    scheduler, path, tools = _make_with_stop(tmp_path, lambda task_id: (1, False))
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="s")

    out = await tools["stop_scheduled_task"]("s")

    assert "Stopped" in out and "s" in out
    assert "schedule" in out.lower()
    assert scheduling.load(path)[0]["enabled"] is True


async def test_stop_scheduled_task_names_the_count_when_several_runs_were_in_flight(tmp_path):
    """A manual run-now alongside an armed firing means a task can have more than one run going."""
    scheduler, path, tools = _make_with_stop(tmp_path, lambda task_id: (2, False))
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="s")

    assert "2 runs" in await tools["stop_scheduled_task"]("s")


async def test_stop_scheduled_task_reports_a_task_that_was_not_running(tmp_path):
    """Distinct from a stop that worked: the model should not tell the user it stopped something."""
    scheduler, path, tools = _make_with_stop(tmp_path, lambda task_id: (0, False))
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="s")

    out = await tools["stop_scheduled_task"]("s")

    assert "not running" in out.lower() and "Stopped" not in out


async def test_stop_scheduled_task_says_when_the_only_run_is_the_one_asking(tmp_path):
    """A task's own firing asking to stop itself is left alone, so the sentence has to explain the
    difference from 'nothing was running' rather than claim the task is idle."""
    scheduler, path, tools = _make_with_stop(tmp_path, lambda task_id: (0, True))
    await tools["schedule_task"]("ping", "interval", interval_seconds=60, name="s")

    out = await tools["stop_scheduled_task"]("s")

    assert "this run" in out.lower() and "not running" not in out.lower()


async def test_stop_scheduled_task_reports_an_unknown_handle(tmp_path):
    scheduler, path, tools = _make_with_stop(tmp_path, lambda task_id: (1, False))

    assert "No scheduled task" in await tools["stop_scheduled_task"]("nope")
