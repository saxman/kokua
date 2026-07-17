"""Durable, agent-managed scheduled tasks.

AIMU's ``Scheduler`` runs in-memory jobs and is deliberately non-persistent; this module is the
"durable wrapper above the library" it defers to. It owns a tolerant JSON registry of tasks (mirroring
``mcp_registry.py``), the ``next_fire`` scheduler math for the supported recurrence types, and the
``make_scheduler_tools`` factory that binds the agent tools to the live ``Scheduler`` and the
assistant's proactive-turn method (mirroring ``mcp.make_mcp_tools``).
"""

from __future__ import annotations

import functools
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from aimu.tools import tool

logger = logging.getLogger(__name__)


WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hhmm(value) -> tuple[int, int]:
    try:
        hour_str, minute_str = value.split(":")
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        raise ValueError(f"time must be 'HH:MM', got {value!r}")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"time out of range: {value!r}")
    return hour, minute


def next_fire(schedule: dict, now: datetime) -> Optional[float]:
    """Seconds from ``now`` to the next occurrence of ``schedule``.

    Returns ``None`` for a ``once`` schedule whose time has already passed (used to drop past-due
    one-shots). Raises ``ValueError`` on a malformed schedule so callers can surface an actionable
    message rather than a traceback.
    """
    kind = schedule.get("type")
    if kind == "once":
        raw = schedule.get("at")
        try:
            at = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            raise ValueError(f"once.at must be an ISO-8601 datetime, got {raw!r}")
        delta = (at - now).total_seconds()
        return delta if delta > 0 else None
    if kind == "interval":
        seconds = schedule.get("seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 1:
            raise ValueError("interval.seconds must be a number >= 1")
        return float(seconds)
    if kind == "daily":
        hour, minute = _parse_hhmm(schedule.get("at"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()
    if kind == "weekly":
        day = schedule.get("day")
        if day not in WEEKDAYS:
            raise ValueError(f"weekly.day must be one of {list(WEEKDAYS)}")
        hour, minute = _parse_hhmm(schedule.get("at"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_offset = (WEEKDAYS[day] - now.weekday()) % 7
        if days_offset == 0 and target <= now:
            return 7 * 24 * 3600.0
        target += timedelta(days=days_offset)
        if target <= now:
            target += timedelta(days=7)
        return (target - now).total_seconds()
    raise ValueError(f"unknown schedule type {kind!r}")


def load(path: Path) -> list[dict]:
    """Return the persisted task records (``[]`` if the file is absent or unreadable)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read scheduled-task registry %s; ignoring it.", path, exc_info=True)
        return []
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, dict) and record.get("id")]


def add(path: Path, record: dict) -> None:
    """Append a task record, replacing any existing record with the same id."""
    records = [r for r in load(path) if r.get("id") != record["id"]]
    records.append(record)
    _write(path, records)


def remove(path: Path, task_id: str) -> bool:
    """Drop a task by id. Returns whether a record was actually removed."""
    records = load(path)
    kept = [r for r in records if r.get("id") != task_id]
    if len(kept) == len(records):
        return False
    _write(path, kept)
    return True


def find(records: list[dict], id_or_name: str) -> Optional[dict]:
    """Resolve a handle to a record, matching on id first, then name."""
    for record in records:
        if record.get("id") == id_or_name:
            return record
    for record in records:
        if record.get("name") == id_or_name:
            return record
    return None


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def _build_schedule(
    schedule_type: str,
    time_of_day: Optional[str],
    at_datetime: Optional[str],
    interval_seconds: Optional[float],
    weekday: Optional[str],
) -> dict:
    """Assemble the persisted schedule dict from the flat ``schedule_task`` tool arguments.

    Splitting the schedule into named scalar arguments (rather than one opaque ``dict``) is what lets
    the model fill it reliably: the tool schema advertises each field by name. Raises ``ValueError`` on
    an unknown ``schedule_type`` (missing per-type fields are caught later by ``next_fire``).
    """
    kind = (schedule_type or "").strip().lower()
    if kind == "once":
        return {"type": "once", "at": at_datetime}
    if kind == "interval":
        return {"type": "interval", "seconds": interval_seconds}
    if kind == "daily":
        return {"type": "daily", "at": time_of_day}
    if kind == "weekly":
        day = weekday.strip().lower()[:3] if isinstance(weekday, str) else weekday
        return {"type": "weekly", "day": day, "at": time_of_day}
    raise ValueError(f"schedule_type must be one of once, interval, daily, weekly; got {schedule_type!r}")


def make_scheduler_tools(
    scheduler,
    registry_path: Path,
    fire: Callable[..., Awaitable[None]],
) -> tuple[list[Callable], Callable[[], None]]:
    """Build the schedule/list/cancel agent tools bound to a live ``Scheduler`` and a fire callback.

    ``fire`` is the assistant's proactive-turn entry point, called as
    ``await fire(prompt, new_session=..., task_name=...)`` when a task is due. Returns the tool list
    plus ``arm_all`` (call once at boot to schedule persisted tasks).
    """

    def _arm(record: dict) -> bool:
        delay = next_fire(record["schedule"], datetime.now())
        if delay is None:  # past-due one-shot
            return False
        scheduler.at(delay, functools.partial(_fire_job, record["id"]), name=record["id"])
        return True

    async def _fire_job(task_id: str) -> None:
        record = find(load(registry_path), task_id)
        if record is None:  # cancelled between arming and firing
            return
        try:
            await fire(record["prompt"], new_session=record.get("new_session", False), task_name=record.get("name"))
        finally:
            # Re-read the registry: a cancel during the run (which removes the record) must win over
            # the re-arm, and any edit is picked up. Recurring tasks re-arm; one-shots are dropped.
            current = find(load(registry_path), task_id)
            if current is not None:
                if current["schedule"].get("type") == "once":
                    remove(registry_path, task_id)
                elif current.get("enabled", True):  # a disable during the run wins over the re-arm
                    _arm(current)

    def arm_all() -> None:
        for record in load(registry_path):
            if not record.get("enabled", True):
                continue
            if not _arm(record) and record["schedule"].get("type") == "once":
                remove(registry_path, record["id"])
                logger.info("Dropped past-due one-shot scheduled task %s", record["id"])

    @tool
    async def schedule_task(
        prompt: str,
        schedule_type: Literal["once", "interval", "daily", "weekly"],
        time_of_day: Optional[str] = None,
        at_datetime: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        weekday: Optional[str] = None,
        name: Optional[str] = None,
        new_session: bool = False,
    ) -> str:
        """Schedule a task that runs an unprompted assistant turn with the given prompt when it is due.

        Args:
            prompt: The instruction to run when the task fires.
            schedule_type: One of "once", "interval", "daily", or "weekly".
            time_of_day: For "daily" or "weekly", a 24-hour "HH:MM", e.g. "20:00".
            at_datetime: For "once", an ISO-8601 local datetime, e.g. "2026-07-16T17:00:00".
            interval_seconds: For "interval", the number of seconds between runs (>= 1).
            weekday: For "weekly", one of mon/tue/wed/thu/fri/sat/sun.
            name: Optional unique handle to cancel the task later.
            new_session: If true, run each firing in its own new conversation so the user can review it.
        """
        try:
            schedule = _build_schedule(schedule_type, time_of_day, at_datetime, interval_seconds, weekday)
            delay = next_fire(schedule, datetime.now())
        except ValueError as exc:
            return f"Invalid schedule: {exc}"
        if delay is None:
            return "That time is in the past; choose a future time."
        records = load(registry_path)
        if name and find(records, name) is not None:
            return f"A task named {name!r} already exists; cancel it first or use a different name."
        record = {
            "id": uuid.uuid4().hex,
            "name": name,
            "prompt": prompt,
            "schedule": schedule,
            "new_session": bool(new_session),
            "created_at": datetime.now().isoformat(),
            "enabled": True,
        }
        add(registry_path, record)
        _arm(record)
        return f"Scheduled task {record['id']} ({name or 'unnamed'}); first run in ~{int(delay)}s."

    @tool
    async def list_scheduled_tasks() -> str:
        """List the scheduled tasks: id, name, schedule, next fire, and prompt."""
        records = load(registry_path)
        if not records:
            return "No scheduled tasks."
        now = datetime.now()
        lines = []
        for record in records:
            if not record.get("enabled", True):
                when = "disabled"
            else:
                try:
                    delay = next_fire(record["schedule"], now)
                except ValueError:
                    delay = None
                when = "past" if delay is None else f"~{int(delay)}s"
            preview = record["prompt"][:60]
            lines.append(
                f"- {record['id']} [{record.get('name') or 'unnamed'}] {record['schedule']} "
                f"next {when} new_session={record.get('new_session', False)}: {preview}"
            )
        return "\n".join(lines)

    @tool
    async def cancel_scheduled_task(id_or_name: str) -> str:
        """Cancel a scheduled task by its id or name."""
        record = find(load(registry_path), id_or_name)
        if record is None:
            return f"No scheduled task matches {id_or_name!r}."
        scheduler.cancel(record["id"])
        remove(registry_path, record["id"])
        return f"Cancelled scheduled task {record['id']} ({record.get('name') or 'unnamed'})."

    @tool
    async def disable_scheduled_task(id_or_name: str) -> str:
        """Disable a scheduled task by id or name: it stops firing but stays in the registry.

        Re-enable it later with ``enable_scheduled_task``. Use ``cancel_scheduled_task`` to remove it.
        """
        record = find(load(registry_path), id_or_name)
        if record is None:
            return f"No scheduled task matches {id_or_name!r}."
        if not record.get("enabled", True):
            return f"Scheduled task {record['id']} ({record.get('name') or 'unnamed'}) is already disabled."
        scheduler.cancel(record["id"])
        record["enabled"] = False
        add(registry_path, record)
        return f"Disabled scheduled task {record['id']} ({record.get('name') or 'unnamed'})."

    @tool
    async def enable_scheduled_task(id_or_name: str) -> str:
        """Re-enable a disabled scheduled task by id or name so it resumes firing on its schedule."""
        record = find(load(registry_path), id_or_name)
        if record is None:
            return f"No scheduled task matches {id_or_name!r}."
        if record.get("enabled", True):
            return f"Scheduled task {record['id']} ({record.get('name') or 'unnamed'}) is already enabled."
        record["enabled"] = True
        add(registry_path, record)
        handle = f"{record['id']} ({record.get('name') or 'unnamed'})"
        if not _arm(record):  # past-due one-shot: flag flipped, but nothing to schedule
            return f"Enabled scheduled task {handle}, but its scheduled time is in the past, so it will not fire."
        return f"Enabled scheduled task {handle}."

    return [
        schedule_task,
        list_scheduled_tasks,
        cancel_scheduled_task,
        disable_scheduled_task,
        enable_scheduled_task,
    ], arm_all
