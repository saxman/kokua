"""Durable, agent-managed scheduled tasks.

AIMU's ``Scheduler`` runs in-memory jobs and is deliberately non-persistent; this module is the
"durable wrapper above the library" it defers to. It owns a tolerant JSON registry of tasks, the
``next_fire`` scheduler math for the supported recurrence types, and the
``make_scheduler_tools`` factory that binds the agent tools to the live ``Scheduler`` and the
assistant's proactive-turn method (mirroring ``mcp.make_mcp_tools``).
"""

from __future__ import annotations

import functools
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from aimu.tools import tool

from .recurrence import next_fire
from .registry import _record_target, add, find, load, remove

logger = logging.getLogger(__name__)


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
    ``await fire(prompt, target=..., task_name=..., session_id=...)`` when a task is due; for a
    ``target="task"`` firing it returns the conversation key it used, which is persisted back onto the
    record so the next firing reuses it. Returns the tool list plus ``arm_all`` (call once at boot to
    schedule persisted tasks).
    """

    def _lookup(id_or_name: str) -> Optional[dict]:
        """Resolve a handle to its current record, re-read from disk each time.

        Always re-read: a cancel or edit between arming and firing (or during a run) must win over
        whatever the caller was holding.
        """
        return find(load(registry_path), id_or_name)

    def _handle(record: dict) -> str:
        """How a task is named back to the model and the user: id plus its optional name."""
        return f"{record['id']} ({record.get('name') or 'unnamed'})"

    def _arm(record: dict) -> bool:
        delay = next_fire(record["schedule"], datetime.now())
        if delay is None:  # past-due one-shot
            return False
        scheduler.at(delay, functools.partial(_fire, record["id"], rearm=True), name=record["id"])
        return True

    def _remember_session(task_id: str, target: str, used_key) -> None:
        # Persist the conversation key a "task"-target firing wrote to, so the next firing reuses it.
        # Re-read the registry so a cancel/delete during the run wins (mirrors the re-arm guard): a
        # write-back must never resurrect a record the user removed mid-run.
        if target != "task" or not used_key:
            return
        current = _lookup(task_id)
        if current is not None and current.get("session_id") != used_key:
            current["session_id"] = used_key
            add(registry_path, current)

    async def _fire(task_id: str, *, rearm: bool) -> None:
        """Run a task's prompt through the proactive path, as a scheduled firing would.

        ``rearm=False`` is a manual "run it now": identical execution, but none of the schedule
        bookkeeping, so running a task by hand must not disturb when it next fires.
        """
        record = _lookup(task_id)
        if record is None:  # cancelled between arming and firing
            return
        target = _record_target(record)
        try:
            used_key = await fire(
                record["prompt"],
                target=target,
                task_name=record.get("name"),
                session_id=record.get("session_id") or None,
            )
            _remember_session(task_id, target, used_key)
        finally:
            if rearm:
                # Re-read the registry: a cancel during the run (which removes the record) must win
                # over the re-arm, and any edit is picked up. Recurring tasks re-arm; one-shots drop.
                current = _lookup(task_id)
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
        target: Literal["active", "new", "task"] = "active",
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
            target: Where each firing runs. "active" (default) uses the currently-viewed conversation.
                "new" runs each firing in its own fresh conversation. "task" gives the task one
                dedicated conversation, created on the first firing and reused on every later firing so
                it builds on its own history.
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
            "target": target,
            "session_id": "",  # populated on the first firing when target == "task"
            "created_at": datetime.now().isoformat(),
            "enabled": True,
        }
        add(registry_path, record)
        _arm(record)
        return f"Scheduled task {_handle(record)}; first run in ~{int(delay)}s."

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
                f"next {when} target={_record_target(record)}: {preview}"
            )
        return "\n".join(lines)

    @tool
    async def cancel_scheduled_task(id_or_name: str) -> str:
        """Cancel a scheduled task by its id or name."""
        record = _lookup(id_or_name)
        if record is None:
            return f"No scheduled task matches {id_or_name!r}."
        scheduler.cancel(record["id"])
        remove(registry_path, record["id"])
        return f"Cancelled scheduled task {_handle(record)}."

    def _set_enabled(id_or_name: str, enabled: bool) -> str:
        """Flip a task's enabled flag and (un)arm it. Shared by the two tools below, which stay
        separate because the model needs two names and two descriptions to choose between."""
        record = _lookup(id_or_name)
        if record is None:
            return f"No scheduled task matches {id_or_name!r}."
        verb = "enabled" if enabled else "disabled"
        if record.get("enabled", True) is enabled:
            return f"Scheduled task {_handle(record)} is already {verb}."
        record["enabled"] = enabled
        add(registry_path, record)
        if not enabled:
            scheduler.cancel(record["id"])
            return f"Disabled scheduled task {_handle(record)}."
        if not _arm(record):  # past-due one-shot: flag flipped, but nothing to schedule
            return (
                f"Enabled scheduled task {_handle(record)}, but its scheduled time is in the past, so it will not fire."
            )
        return f"Enabled scheduled task {_handle(record)}."

    @tool
    async def disable_scheduled_task(id_or_name: str) -> str:
        """Disable a scheduled task by id or name: it stops firing but stays in the registry.

        Re-enable it later with ``enable_scheduled_task``. Use ``cancel_scheduled_task`` to remove it.
        """
        return _set_enabled(id_or_name, False)

    @tool
    async def enable_scheduled_task(id_or_name: str) -> str:
        """Re-enable a disabled scheduled task by id or name so it resumes firing on its schedule."""
        return _set_enabled(id_or_name, True)

    @tool
    async def run_scheduled_task(id_or_name: str) -> str:
        """Run an existing scheduled task now, without changing its schedule.

        Reproduces exactly what the task's next scheduled firing would do (honoring its ``target``;
        gated tools are auto-denied as they would be for an unattended firing), so you can verify how
        the task behaves. A ``target="task"`` run writes into (and, on the first run, creates and
        remembers) the task's dedicated conversation. The task's output arrives as a separate message
        shortly after, not as this tool's return value. Works on a disabled task too.
        """
        record = _lookup(id_or_name)
        if record is None:
            return f"No scheduled task matches {id_or_name!r}."
        # at(0) so the firing runs after the current turn releases the assistant lock: fire() re-acquires
        # that lock, so running it inline here would deadlock. A distinct job name avoids colliding with
        # the record's real armed job (name == id).
        scheduler.at(0, functools.partial(_fire, record["id"], rearm=False), name=f"run-now:{record['id']}")
        handle = _handle(record)
        suffix = " in a new conversation" if _record_target(record) in ("new", "task") else ""
        note = " (note: this task is disabled)" if not record.get("enabled", True) else ""
        return f"Running task {handle} now; its output will appear shortly{suffix}.{note}"

    return [
        schedule_task,
        list_scheduled_tasks,
        cancel_scheduled_task,
        disable_scheduled_task,
        enable_scheduled_task,
        run_scheduled_task,
    ], arm_all
