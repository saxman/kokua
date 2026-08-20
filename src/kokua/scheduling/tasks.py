"""The scheduled-task lifecycle: the operations behind both the agent tools and the web task panel.

AIMU's ``Scheduler`` runs in-memory jobs and is deliberately non-persistent; this module is the
"durable wrapper above the library" it defers to. :class:`TaskService` pairs every config write with
the scheduler (un)arming that must accompany it, which is why it is the only supported way to change a
task's state: a caller that hand-edited config.toml directly would leave the in-memory scheduler firing
a task the file calls disabled.

Nothing here formats a sentence. Operations return records and raise :class:`TaskError`, and each
presentation layer renders its own text -- ``toolsets/scheduling.py`` for the model,
``web_static/app.js`` for the sidebar. The two used to share one string, which meant the panel
displayed prose written to steer a model.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from kokua.config import store
from kokua.config.file import ConfigError

from .recurrence import next_fire

logger = logging.getLogger(__name__)

# What a task's next firing is, when there is no countdown to give. Rendered separately by each
# presentation layer, so the wording can differ between the sidebar and a tool result.
STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"
STATUS_INVALID = "invalid"
STATUS_PAST = "past"


class TaskError(Exception):
    """A task operation could not be completed. One base so a wrapper can catch the whole family."""


class TaskNotFound(TaskError):
    """No task matches the given name. Carries the name that was looked up."""

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


class InvalidRetention(TaskError):
    """A retention cap below zero, which has no meaning. Carries the offending value."""

    def __init__(self, value: object):
        super().__init__(value)
        self.value = value


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
class StopResult:
    """The outcome of :meth:`TaskService.stop`.

    ``stopped`` is how many of the task's runs were cancelled, which is zero for a task that simply had
    nothing in flight. ``skipped_self`` says one of its runs was left alone because the call came from
    inside it; a caller needs both, since "nothing was running" and "the only run is the one asking" are
    different things to report.
    """

    record: dict
    stopped: int
    skipped_self: bool


@dataclass(frozen=True)
class TaskService:
    """Every scheduled-task operation, bound to a live ``Scheduler`` and a proactive-turn callback.

    ``fire`` is the assistant's proactive-turn entry point, called as
    ``await fire(prompt, task_name=..., task_id=..., max_conversations=...)`` when a task is due.
    ``task_id`` is passed so the conversation the firing mints records which task minted it, which is
    both how a front end groups a task's runs and how retention knows which conversations to prune.

    ``default_max_conversations`` is read at fire time rather than captured, so a change to the
    ``[scheduling]`` setting behind it reaches the next firing without a restart. A callable rather
    than the config object keeps this subsystem free of any dependency on the config layer.

    ``stop_run`` cancels a task's in-flight firings, returning ``(how many, whether one of them was the
    run the call came from)``. Injected for the same reason ``fire`` is: which runs are in flight is the
    assistant's bookkeeping, not the store's. Left unset, :meth:`stop` reports nothing running, which
    is the truth for a service with no live turns behind it.

    ``rename_conversations`` re-points the conversations a task minted when the task is renamed,
    returning how many moved. Injected for the same reason ``fire`` and ``stop_run`` are: which
    conversations exist is the assistant's bookkeeping, not the store's. Left unset, a rename moves the
    task and orphans its history, which is the truth for a service with no conversations behind it.
    """

    scheduler: object
    config_path: Path
    fire: Callable[..., Awaitable[None]]
    default_max_conversations: Callable[[], int] = lambda: 0
    stop_run: Optional[Callable[[str], tuple[int, bool]]] = None
    rename_conversations: Optional[Callable[[str, str], int]] = None

    # -- reading ---------------------------------------------------------------------------------

    def _lookup(self, name: str) -> Optional[dict]:
        """Resolve a name to its current record, re-read from the config file each time.

        Always re-read: a cancel or edit between arming and firing (or during a run) must win over
        whatever the caller was holding. Propagates ``ConfigError`` from an unreadable file rather than
        reporting no task, so a broken hand-edit is never mistaken for a cancellation.
        """
        for record in store.load_tasks(self.config_path):
            if record["name"] == name:
                return record
        return None

    def _require(self, name: str) -> dict:
        record = self._lookup(name)
        if record is None:
            raise TaskNotFound(name)
        return record

    def get(self, name: str) -> dict:
        """One task record by name. Raises :class:`TaskNotFound`."""
        return self._require(name)

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

        ``max_conversations`` is the record's own cap, ``None`` when it inherits the configured
        default; the effective value is resolved at fire time, not here.
        """
        now = datetime.now()
        items = []
        for record in store.load_tasks(self.config_path):
            status, seconds = self.next_firing(record, now)
            items.append(
                {
                    "name": record["name"],
                    "prompt": record["prompt"],
                    "schedule": record["schedule"],
                    "max_conversations": record.get("max_conversations"),
                    "enabled": record.get("enabled", True),
                    "created_at": record.get("created_at", ""),
                    "status": status,
                    "next_fire_seconds": seconds,
                }
            )
        return items

    # -- arming and firing -----------------------------------------------------------------------

    def _retire(self, record: dict, *, fired: bool = True) -> None:
        """Mark a spent one-shot disabled in place, rather than deleting the table it lives in.

        A task's table in config.toml may be something the user typed and commented, so a schedule
        merely reaching its end must not erase it: the run stays auditable, and re-running it is a
        one-character edit. ``arm_all`` already skips a disabled record, so nothing more is needed to
        keep it from firing again. ``fired=False`` is a one-shot found past due at boot, which is
        retired for the same reason but never ran.
        """
        record["enabled"] = False
        if fired:
            record["fired_at"] = datetime.now().isoformat()
        store.write_task(self.config_path, record["name"], record)

    def _arm(self, record: dict) -> Optional[float]:
        """Schedule a record's next firing, returning the delay used, or ``None`` if it is past due.

        The schedule is captured into the callback alongside the name so a firing whose re-read fails
        can still re-arm itself: without it, an unrelated syntax error in config.toml at exactly the
        wrong moment would silently retire a recurring task.
        """
        delay = next_fire(record["schedule"], datetime.now())
        if delay is None:  # past-due one-shot
            return None
        self.scheduler.at(
            delay,
            functools.partial(self._fire, record["name"], record["schedule"], rearm=True),
            name=record["name"],
        )
        return delay

    async def _fire(self, name: str, armed_schedule: dict, *, rearm: bool) -> None:
        """Run a task's prompt through the proactive path, as a scheduled firing would.

        ``rearm=False`` is a manual "run it now": identical execution, but none of the schedule
        bookkeeping, so running a task by hand must not disturb when it next fires.
        """
        try:
            record = self._lookup(name)
        except ConfigError:
            logger.warning(
                "Could not read scheduled tasks from %s; skipping this firing of %r.",
                self.config_path,
                name,
                exc_info=True,
            )
            if rearm:
                self._arm({"name": name, "schedule": armed_schedule})
            return
        if record is None:  # cancelled between arming and firing
            return
        try:
            await self.fire(
                record["prompt"],
                task_name=name,
                task_id=name,
                max_conversations=self._retention(record),
            )
        finally:
            if rearm:
                self._rearm(name, armed_schedule)

    def _rearm(self, name: str, armed_schedule: dict) -> None:
        """Decide what happens to a task after one of its firings finishes.

        Re-read the file: a cancel during the run must win over the re-arm, and any edit is picked up.
        A recurring task re-arms, a one-shot retires in place, and a read failure falls back to the
        schedule the job was armed with, so the task survives a config the user is mid-edit on.
        """
        try:
            current = self._lookup(name)
        except ConfigError:
            logger.warning(
                "Could not re-read scheduled tasks from %s; re-arming %r on its previous schedule.",
                self.config_path,
                name,
                exc_info=True,
            )
            self._arm({"name": name, "schedule": armed_schedule})
            return
        if current is None:
            return
        if current["schedule"].get("type") == "once":
            self._retire(current)
        elif current.get("enabled", True):  # a disable during the run wins over the re-arm
            self._arm(current)

    def arm_all(self) -> None:
        """Schedule every enabled task declared in config.toml. Called once at boot, before any agent exists."""
        for record in store.load_tasks(self.config_path):
            if not record.get("enabled", True):
                continue
            if self._arm(record) is None and record["schedule"].get("type") == "once":
                self._retire(record, fired=False)
                logger.info("Retired past-due one-shot scheduled task %s", record["name"])

    # -- mutating --------------------------------------------------------------------------------

    def _retention(self, record: dict) -> int:
        """How many conversations this task keeps: its own cap, or the configured default.

        ``0`` is a real value (unlimited), so an absent key is the only thing that inherits.
        """
        own = record.get("max_conversations")
        return self.default_max_conversations() if own is None else int(own)

    @staticmethod
    def _validate_retention(value: Optional[int]) -> None:
        if value is not None and value < 0:
            raise InvalidRetention(value)

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
        name: str,
        max_conversations: Optional[int] = None,
    ) -> tuple[dict, float]:
        """Persist and arm a new task under ``name``. Returns ``(record, seconds to its first firing)``.

        ``name`` is required because it is the task's identity: it is the table key in config.toml and
        the ``task_id`` stamped on every conversation the task mints. A caller with no name of its own
        derives one before calling (see ``toolsets/scheduling.py``).

        ``max_conversations`` left as ``None`` inherits the configured default at fire time, so a task
        created before the default changed follows the new one.

        Raises ``ValueError`` if ``name`` is empty or whitespace-only, since a blank identity is not a
        name a rename could ever produce, and it would still be stamped as the ``task_id`` on every
        conversation the task mints. Also raises :class:`ScheduleInvalid`, :class:`SchedulePast`,
        :class:`DuplicateName`, or :class:`InvalidRetention`, each before anything is written, so a
        rejected call leaves the file untouched.
        """
        delay = self._validate(schedule)
        self._validate_retention(max_conversations)
        stripped_name = name.strip()
        if not stripped_name:
            raise ValueError("a task's name cannot be empty")
        if self._lookup(stripped_name) is not None:
            raise DuplicateName(stripped_name)
        record = {
            "name": stripped_name,
            "prompt": prompt,
            "schedule": schedule,
            "max_conversations": max_conversations,
            "created_at": datetime.now().isoformat(),
            "enabled": True,
        }
        store.write_task(self.config_path, stripped_name, record)
        self._arm(record)
        return record, delay

    def update(
        self,
        name: str,
        *,
        prompt: Optional[str] = None,
        schedule: Optional[dict] = None,
        new_name: Optional[str] = None,
        max_conversations: Optional[int] = None,
    ) -> tuple[dict, list[str]]:
        """Edit a task in place, keeping its history and every field left as ``None``.

        ``name`` looks the task up; ``new_name``, if given, renames it. The two cannot share one
        parameter: ``name`` is required (it is how the record is found) while a rename is optional, so
        collapsing them would make "no rename" indistinguishable from "rename to the current name".

        Returns ``(record, changed field names)``; an empty list means the call was a no-op. The task is
        re-armed only when the schedule actually changes or the task was renamed, so editing a prompt
        never resets an interval countdown, and a disabled task is never armed by an edit.

        Every rejection is raised before the first mutation, so a partially valid edit applies nothing.
        """
        record = self._require(name)
        self._validate_retention(max_conversations)
        changed: list[str] = []

        merged = record["schedule"]
        if schedule is not None:
            self._validate(schedule)
            merged = schedule
            if merged != record["schedule"]:
                changed.append("schedule")

        old_name: Optional[str] = None
        if new_name is not None:
            stripped_name = new_name.strip()
            if stripped_name and stripped_name != record["name"]:
                if self._lookup(stripped_name) is not None:
                    raise DuplicateName(stripped_name)
                old_name = record["name"]
                record["name"] = stripped_name
                changed.append("name")
        if prompt is not None and prompt != record["prompt"]:
            record["prompt"] = prompt
            changed.append("prompt")
        if max_conversations is not None and max_conversations != record.get("max_conversations"):
            record["max_conversations"] = max_conversations
            changed.append("max_conversations")

        if not changed:
            return record, changed
        record["schedule"] = merged
        if old_name is not None:
            store.rename_task(self.config_path, old_name, record["name"])
            if self.rename_conversations:
                self.rename_conversations(old_name, record["name"])
            # The scheduler keys a job by the task's name, so a rename must cancel the old job even
            # when the schedule itself is unchanged, or the task fires under a name nothing can stop.
            self.scheduler.cancel(old_name)
        store.write_task(self.config_path, record["name"], record)
        if record.get("enabled", True) and ("schedule" in changed or old_name is not None):
            self.scheduler.cancel(record["name"])
            self._arm(record)
        return record, changed

    def cancel(self, name: str) -> dict:
        """Disarm and delete a task's table. Returns the record as it was. Raises :class:`TaskNotFound`.

        Deletes the table outright: cancelling is an explicit instruction to forget the task, which is
        why it differs from a spent one-shot reaching the end of its own schedule, which is left in
        place rather than deleted.
        """
        record = self._require(name)
        self.scheduler.cancel(name)
        if not store.remove_task(self.config_path, name):
            # The read that found the task and the write that deletes it walk the file differently, so a
            # shape one accepts and the other misses would leave the task on disk to return at the next
            # startup, with the scheduler already told to forget it. Nothing here can repair that; the
            # log is what keeps it from being silent.
            logger.warning(
                "Scheduled task %r was disarmed but no table for it was found in %s; it may return on "
                "the next restart.",
                name,
                self.config_path,
            )
        return record

    def set_enabled(self, name: str, enabled: bool) -> EnabledResult:
        """Flip a task's enabled flag and (un)arm it to match. Raises :class:`TaskNotFound`."""
        record = self._require(name)
        if record.get("enabled", True) is enabled:
            return EnabledResult(record, changed=False, armed=enabled)
        record["enabled"] = enabled
        store.write_task(self.config_path, name, record)
        if not enabled:
            self.scheduler.cancel(name)
            return EnabledResult(record, changed=True, armed=False)
        return EnabledResult(record, changed=True, armed=self._arm(record) is not None)

    def stop(self, name: str) -> StopResult:
        """Cancel whatever runs of a task are in flight, leaving its schedule armed.

        Not a state change, which is why nothing here is written: a recurring task fires again on its
        normal cadence afterwards, and a one-shot is still consumed. Use :meth:`set_enabled` to keep it
        from coming back. Raises :class:`TaskNotFound`.
        """
        record = self._require(name)
        stopped, skipped_self = self.stop_run(name) if self.stop_run else (0, False)
        return StopResult(record, stopped, skipped_self)

    def run_now(self, name: str) -> dict:
        """Enqueue a task to run immediately, without changing its schedule.

        The firing is enqueued rather than awaited: ``fire`` re-acquires the assistant lock that the
        caller's own turn is holding, so running it inline here would deadlock. ``at(0)`` runs it once
        the current turn releases that lock, under a distinct job name so it cannot collide with the
        record's real armed job (whose name is the task's).
        """
        record = self._require(name)
        self.scheduler.at(
            0,
            functools.partial(self._fire, name, record["schedule"], rearm=False),
            name=f"run-now:{name}",
        )
        return record
