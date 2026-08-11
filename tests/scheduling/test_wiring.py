"""Scheduled tasks wired into a live assistant."""

from __future__ import annotations


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


async def test_create_arms_persisted_tasks_and_drops_past_once(tmp_path):
    from kokua import scheduling

    cfg = _config(tmp_path)
    scheduling.add(
        cfg.scheduled_tasks_path,
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
    await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    # Past-due one-shot was dropped during boot arming.
    assert scheduling.load(cfg.scheduled_tasks_path) == []
