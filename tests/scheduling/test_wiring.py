"""Scheduled tasks wired into a live assistant."""

from __future__ import annotations

from dataclasses import replace

from kokua.config import store
from kokua.core.assistant import Assistant
from tests.channels import FakeChannel, _config
from tests.helpers import MockAsyncModelClient


async def test_create_registers_scheduling_tools(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {getattr(fn, "__name__", None) for fn in assistant._agent.tools}
    assert {
        "schedule_task",
        "list_scheduled_tasks",
        "get_scheduled_task",
        "update_scheduled_task",
        "cancel_scheduled_task",
    } <= names


async def test_list_tasks_reports_the_persisted_config(tmp_path):
    cfg = _config(tmp_path)
    store.write_task(
        cfg.config_path,
        "brief",
        {
            "prompt": "summarize inbox",
            "schedule": {"type": "interval", "seconds": 3600},
            "max_conversations": 1,
            "created_at": "2026-08-01T00:00:00",
            "enabled": True,
        },
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    items = assistant.list_tasks()

    assert len(items) == 1 and items[0]["name"] == "brief"


async def test_task_action_disables_a_task_through_the_shared_service(tmp_path):
    """The panel's actions go through the one TaskService, so the file and the live scheduler agree."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    store.write_task(
        cfg.config_path,
        "brief",
        {
            "prompt": "p",
            "schedule": {"type": "interval", "seconds": 3600},
            "created_at": "x",
            "enabled": True,
        },
    )

    assistant.task_action("disable", "brief")

    assert store.load_tasks(cfg.config_path)[0]["enabled"] is False
    assert assistant.list_tasks()[0]["status"] == "disabled"


async def test_task_action_rejects_an_unknown_action(tmp_path):
    """The action arrives from the browser, so it is checked against an allowlist rather than
    dispatched on whatever string was sent."""
    import pytest

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    with pytest.raises(ValueError):
        assistant.task_action("drop_table", "brief")


async def test_create_arms_persisted_tasks_and_retires_past_once(tmp_path):
    cfg = _config(tmp_path)
    store.write_task(
        cfg.config_path,
        "o",
        {
            "prompt": "p",
            "schedule": {"type": "once", "at": "2000-01-01T00:00:00"},
            "created_at": "x",
            "enabled": True,
        },
    )
    await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    # Past-due one-shot was retired in place during boot arming, not deleted.
    record = store.load_tasks(cfg.config_path)[0]
    assert record["name"] == "o" and record["enabled"] is False


async def test_the_configured_default_cap_reaches_the_task_service(tmp_path):
    """The default is read at fire time, not captured at construction, so a settings change reaches
    the next firing without a restart."""
    from tests.channels import planning_settings

    cfg = _config(tmp_path, toolset_settings={**planning_settings(), "scheduling": {"max_task_conversations": 2}})
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    assert assistant._state.tasks.default_max_conversations() == 2

    cfg.toolset_settings["scheduling"]["max_task_conversations"] = 5
    assert assistant._state.tasks.default_max_conversations() == 5


async def test_a_config_without_a_scheduling_section_falls_back_to_the_declared_default(tmp_path):
    """Only ``resolve_config`` seeds a section for every declared toolset, so a config built any other
    way must still produce the cap the toolset declares rather than an unlimited one."""
    from kokua.toolsets.scheduling import DEFAULT_MAX_TASK_CONVERSATIONS

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assert assistant._state.tasks.default_max_conversations() == DEFAULT_MAX_TASK_CONVERSATIONS


async def test_task_action_stops_a_running_firing_through_the_shared_service(tmp_path):
    """The panel's Stop button reaches the tracker the same way its other buttons reach the file:
    through the one TaskService, so the tool and the panel cannot disagree about what stopping means."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    store.write_task(
        cfg.config_path,
        "brief",
        {
            "prompt": "p",
            "schedule": {"type": "interval", "seconds": 3600},
            "created_at": "x",
            "enabled": True,
        },
    )
    asked: list[str] = []
    assistant._tasks = replace(assistant._tasks, stop_run=lambda name: (asked.append(name), (1, False))[1])

    assistant.task_action("stop", "brief")

    assert asked == ["brief"]
    assert store.load_tasks(cfg.config_path)[0].get("enabled", True) is True  # a stop is not a disable


async def test_the_task_service_stops_runs_through_the_assistants_tracker(tmp_path):
    """The service's canceller is wired to the live tracker, which is the half a unit test of either
    side cannot see."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assert assistant._tasks.stop_run.__self__ is assistant
