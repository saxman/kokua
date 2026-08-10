"""Recurrence math for the four supported schedule types.

Pure and stateless: seconds until the next occurrence of a schedule, or None for a one-shot whose
time has passed. The opinions baked in here (four types, local timezone, weekly semantics) are the
app's, which is why this is not in AIMU beside its Scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


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
