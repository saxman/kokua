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
from typing import Awaitable, Callable, Optional

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
                else:
                    _arm(current)

    def arm_all() -> None:
        for record in load(registry_path):
            if not _arm(record) and record["schedule"].get("type") == "once":
                remove(registry_path, record["id"])
                logger.info("Dropped past-due one-shot scheduled task %s", record["id"])

    @tool
    async def schedule_task(prompt: str, schedule: dict, name: Optional[str] = None, new_session: bool = False) -> str:
        """Schedule a task that will run an unprompted assistant turn with ``prompt`` when it is due.

        ``schedule`` is a dict of exactly one of these shapes:
          - {"type": "once", "at": "2026-07-15T17:00:00"}   (ISO-8601 local datetime, one time)
          - {"type": "interval", "seconds": 3600}            (every N seconds, N >= 1)
          - {"type": "daily", "at": "09:00"}                 (every day at HH:MM, 24h local time)
          - {"type": "weekly", "day": "mon", "at": "09:00"}  (day is mon/tue/wed/thu/fri/sat/sun)

        ``name`` is an optional unique handle for cancelling later. Set ``new_session`` to run each
        firing in its own new conversation (so the user can review the output and follow up on it)
        instead of the currently-active conversation. Returns a confirmation with the task id.
        """
        try:
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

    return [schedule_task, list_scheduled_tasks, cancel_scheduled_task], arm_all
