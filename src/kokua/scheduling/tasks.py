"""The scheduled-task lifecycle: the operations behind both the agent tools and the web task panel.

AIMU's ``Scheduler`` runs in-memory jobs and is deliberately non-persistent; this module is the
"durable wrapper above the library" it defers to. :class:`TaskService` pairs every registry write with
the scheduler (un)arming that must accompany it, which is why it is the only supported way to change a
task's state: a caller that edited the JSON directly would leave the in-memory scheduler firing a task
the registry calls disabled.

Nothing here formats a sentence. Operations return records and raise :class:`TaskError`, and each
presentation layer renders its own text -- ``toolsets/scheduling.py`` for the model,
``web_static/app.js`` for the sidebar. The two used to share one string, which meant the panel
displayed prose written to steer a model.
"""

from __future__ import annotations

import functools
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .recurrence import next_fire
from .registry import record_target, add, find, load, remove

logger = logging.getLogger(__name__)

TARGETS = ("active", "new", "task", "latest")

# What a task's next firing is, when there is no countdown to give. Rendered separately by each
# presentation layer, so the wording can differ between the sidebar and a tool result.
STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"
STATUS_INVALID = "invalid"
STATUS_PAST = "past"


class TaskError(Exception):
    """A task operation could not be completed. One base so a wrapper can catch the whole family."""


class TaskNotFound(TaskError):
    """No task matches the given id or name. Carries the handle that was looked up."""

    def __init__(self, handle: str):
        super().__init__(handle)
        self.handle = handle


class ScheduleInvalid(TaskError):
    """A schedule cannot be interpreted. Its message is the reason, ready to interpolate."""


class SchedulePast(TaskError):
    """A schedule resolves to a time that has already passed, so there is nothing to arm."""


class DuplicateName(TaskError):
    """Another task already holds this name. Carries the name, since names are the user's handle."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


class InvalidTarget(TaskError):
    """A target outside :data:`TARGETS`. Carries the offending value."""

    def __init__(self, target: object):
        super().__init__(target)
        self.target = target


@dataclass(frozen=True)
class EnabledResult:
    """The outcome of :meth:`TaskService.set_enabled`.

    ``changed`` is False when the task was already in the requested state, and ``armed`` says whether
    enabling actually scheduled a job -- it is False for a past-due one-shot, whose flag flips but which
    still has nothing to fire. A caller needs both to describe what happened without re-reading state.
    """

    record: dict
    changed: bool
    armed: bool


@dataclass(frozen=True)
class TaskService:
    """Every scheduled-task operation, bound to a live ``Scheduler`` and a proactive-turn callback.

    ``fire`` is the assistant's proactive-turn entry point, called as
    ``await fire(prompt, target=..., task_name=..., session_id=..., task_id=...)`` when a task is due;
    for a ``target="task"`` firing it returns the conversation key it used, which is persisted back onto
    the record so the next firing reuses it. ``task_id`` is passed so a conversation the firing mints
    records which task minted it.
    """

    scheduler: object
    registry_path: Path
    fire: Callable[..., Awaitable[None]]

    # -- reading ---------------------------------------------------------------------------------

    def _lookup(self, id_or_name: str) -> Optional[dict]:
        """Resolve a handle to its current record, re-read from disk each time.

        Always re-read: a cancel or edit between arming and firing (or during a run) must win over
        whatever the caller was holding.
        """
        return find(load(self.registry_path), id_or_name)

    def _require(self, id_or_name: str) -> dict:
        record = self._lookup(id_or_name)
        if record is None:
            raise TaskNotFound(id_or_name)
        return record

    def get(self, id_or_name: str) -> dict:
        """One task record by id or name. Raises :class:`TaskNotFound`."""
        return self._require(id_or_name)

    def next_firing(self, record: dict, now: Optional[datetime] = None) -> tuple[str, Optional[float]]:
        """``(status, seconds)`` for a record: one of the ``STATUS_*`` values, plus a countdown.

        The seconds are present only for ``STATUS_PENDING``; the other three statuses are exactly the
        cases with nothing to count down to, which is why they are returned as a status rather than as
        the prose each caller would otherwise have to parse back out.
        """
        if not record.get("enabled", True):
            return STATUS_DISABLED, None
        try:
            delay = next_fire(record["schedule"], now or datetime.now())
        except ValueError:
            return STATUS_INVALID, None
        return (STATUS_PAST, None) if delay is None else (STATUS_PENDING, delay)

    def list(self) -> list[dict]:
        """Every task as fields, with its ``status`` and ``next_fire_seconds`` derived once.

        ``session_id`` is included because it is the only link to the conversation of a
        ``target="task"`` task created before conversations recorded their ``task_id``.
        """
        now = datetime.now()
        items = []
        for record in load(self.registry_path):
            status, seconds = self.next_firing(record, now)
            items.append(
                {
                    "id": record["id"],
                    "name": record.get("name"),
                    "prompt": record["prompt"],
                    "schedule": record["schedule"],
                    "target": record_target(record),
                    "enabled": record.get("enabled", True),
                    "created_at": record.get("created_at", ""),
                    "session_id": record.get("session_id") or None,
                    "status": status,
                    "next_fire_seconds": seconds,
                }
            )
        return items

    # -- arming and firing -----------------------------------------------------------------------

    def _arm(self, record: dict) -> Optional[float]:
        """Schedule a record's next firing, returning the delay used, or ``None`` if it is past due."""
        delay = next_fire(record["schedule"], datetime.now())
        if delay is None:  # past-due one-shot
            return None
        self.scheduler.at(delay, functools.partial(self._fire, record["id"], rearm=True), name=record["id"])
        return delay

    def _remember_session(self, task_id: str, target: str, used_key) -> None:
        # Persist the conversation key a firing used, for the two targets that need it next time:
        # "task" reuses it, and "latest" deletes it. Re-read the registry so a cancel/delete during
        # the run wins (mirrors the re-arm guard): a write-back must never resurrect a record the
        # user removed mid-run.
        if target not in ("task", "latest") or not used_key:
            return
        current = self._lookup(task_id)
        if current is not None and current.get("session_id") != used_key:
            current["session_id"] = used_key
            add(self.registry_path, current)

    async def _fire(self, task_id: str, *, rearm: bool) -> None:
        """Run a task's prompt through the proactive path, as a scheduled firing would.

        ``rearm=False`` is a manual "run it now": identical execution, but none of the schedule
        bookkeeping, so running a task by hand must not disturb when it next fires.
        """
        record = self._lookup(task_id)
        if record is None:  # cancelled between arming and firing
            return
        target = record_target(record)
        try:
            used_key = await self.fire(
                record["prompt"],
                target=target,
                task_name=record.get("name"),
                session_id=record.get("session_id") or None,
                task_id=task_id,
            )
            self._remember_session(task_id, target, used_key)
        finally:
            if rearm:
                # Re-read the registry: a cancel during the run (which removes the record) must win
                # over the re-arm, and any edit is picked up. Recurring tasks re-arm; one-shots drop.
                current = self._lookup(task_id)
                if current is not None:
                    if current["schedule"].get("type") == "once":
                        remove(self.registry_path, task_id)
                    elif current.get("enabled", True):  # a disable during the run wins over the re-arm
                        self._arm(current)

    def arm_all(self) -> None:
        """Schedule every enabled persisted task. Called once at boot, before any agent exists."""
        for record in load(self.registry_path):
            if not record.get("enabled", True):
                continue
            if self._arm(record) is None and record["schedule"].get("type") == "once":
                remove(self.registry_path, record["id"])
                logger.info("Dropped past-due one-shot scheduled task %s", record["id"])

    # -- mutating --------------------------------------------------------------------------------

    def _validate(self, schedule: dict) -> float:
        """The delay a schedule resolves to, rejecting one that cannot be armed."""
        try:
            delay = next_fire(schedule, datetime.now())
        except ValueError as exc:
            raise ScheduleInvalid(str(exc)) from exc
        if delay is None:
            raise SchedulePast(schedule)
        return delay

    def create(
        self,
        prompt: str,
        schedule: dict,
        *,
        name: Optional[str] = None,
        target: str = "active",
    ) -> tuple[dict, float]:
        """Persist and arm a new task. Returns ``(record, seconds until its first firing)``.

        Raises :class:`ScheduleInvalid`, :class:`SchedulePast`, or :class:`DuplicateName`, each before
        anything is written, so a rejected call leaves the registry untouched.
        """
        delay = self._validate(schedule)
        if name and find(load(self.registry_path), name) is not None:
            raise DuplicateName(name)
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
        add(self.registry_path, record)
        self._arm(record)
        return record, delay

    def update(
        self,
        id_or_name: str,
        *,
        prompt: Optional[str] = None,
        schedule: Optional[dict] = None,
        name: Optional[str] = None,
        target: Optional[str] = None,
    ) -> tuple[dict, list[str]]:
        """Edit a task in place, keeping its id, history, and every field left as ``None``.

        Returns ``(record, changed field names)``; an empty list means the call was a no-op. The task is
        re-armed only when the schedule actually changes, so editing a prompt never resets an interval
        countdown, and a disabled task is never armed by an edit.

        Every rejection is raised before the first mutation, so a partially valid edit applies nothing.
        """
        record = self._require(id_or_name)
        changed: list[str] = []

        merged = record["schedule"]
        if schedule is not None:
            self._validate(schedule)
            merged = schedule
            if merged != record["schedule"]:
                changed.append("schedule")

        if name is not None:
            new_name = name.strip() or None
            if new_name != record.get("name"):
                if new_name and find(load(self.registry_path), new_name) is not None:
                    raise DuplicateName(new_name)
                record["name"] = new_name
                changed.append("name")
        if prompt is not None and prompt != record["prompt"]:
            record["prompt"] = prompt
            changed.append("prompt")
        if target is not None:
            if target not in TARGETS:
                raise InvalidTarget(target)
            if target != record_target(record):
                # The dedicated conversation is kept even when the target moves off "task", so flipping
                # back resumes that history instead of minting a second one. Moving *onto* "latest" is
                # the exception: there the remembered key is what the next firing deletes, so keeping it
                # would make the switch quietly destroy the history the task built up under "task".
                if target == "latest":
                    record["session_id"] = ""
                record["target"] = target
                changed.append("target")

        if not changed:
            return record, changed
        record["schedule"] = merged
        add(self.registry_path, record)
        if record.get("enabled", True) and "schedule" in changed:
            self.scheduler.cancel(record["id"])
            self._arm(record)
        return record, changed

    def cancel(self, id_or_name: str) -> dict:
        """Disarm and forget a task. Returns the record as it was. Raises :class:`TaskNotFound`."""
        record = self._require(id_or_name)
        self.scheduler.cancel(record["id"])
        remove(self.registry_path, record["id"])
        return record

    def set_enabled(self, id_or_name: str, enabled: bool) -> EnabledResult:
        """Flip a task's enabled flag and (un)arm it to match. Raises :class:`TaskNotFound`."""
        record = self._require(id_or_name)
        if record.get("enabled", True) is enabled:
            return EnabledResult(record, changed=False, armed=enabled)
        record["enabled"] = enabled
        add(self.registry_path, record)
        if not enabled:
            self.scheduler.cancel(record["id"])
            return EnabledResult(record, changed=True, armed=False)
        return EnabledResult(record, changed=True, armed=self._arm(record) is not None)

    def run_now(self, id_or_name: str) -> dict:
        """Enqueue a task to run immediately, without changing its schedule.

        The firing is enqueued rather than awaited: ``fire`` re-acquires the assistant lock that the
        caller's own turn is holding, so running it inline here would deadlock. ``at(0)`` runs it once
        the current turn releases that lock, under a distinct job name so it cannot collide with the
        record's real armed job (whose name is the task id).
        """
        record = self._require(id_or_name)
        self.scheduler.at(0, functools.partial(self._fire, record["id"], rearm=False), name=f"run-now:{record['id']}")
        return record
