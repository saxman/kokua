"""Durable, agent-managed scheduled tasks.

AIMU's ``Scheduler`` runs in-memory jobs and is deliberately non-persistent; this module is the
"durable wrapper above the library" it defers to. It owns a tolerant JSON registry of tasks (mirroring
``mcp_registry.py``), the ``next_fire`` scheduler math for the supported recurrence types, and the
``make_scheduler_tools`` factory that binds the agent tools to the live ``Scheduler`` and the
assistant's proactive-turn method (mirroring ``mcp.make_mcp_tools``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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
