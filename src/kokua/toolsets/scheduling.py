"""The ``scheduling`` toolset: agent tools over :class:`kokua.scheduling.tasks.TaskService`.

Everything here is presentation for a model. The lifecycle logic lives in ``scheduling/tasks.py``,
which returns records and raises ``TaskError``; this module owns the tool schemas, the docstrings that
steer the model, and every sentence it reads back.

The flat scalar schedule arguments are the clearest example of why the split falls this way. They are
not how a schedule is stored, they are how a model can reliably fill one: the tool schema advertises
``schedule_type``, ``time_of_day``, ``weekday`` and so on by name, where one opaque ``dict`` parameter
was filled wrongly. So ``_build_schedule`` and ``_flatten_schedule`` belong to the tool surface, and
``TaskService`` takes the assembled dict.
"""

from __future__ import annotations

from typing import Callable, Literal, Optional

from aimu.tools import tool

from kokua.scheduling.tasks import (
    STATUS_DISABLED,
    STATUS_INVALID,
    STATUS_PAST,
    DuplicateName,
    InvalidTarget,
    ScheduleInvalid,
    SchedulePast,
    TaskNotFound,
    TaskService,
)
from kokua.toolsets.registry import Toolset

PROMPT_PREVIEW_CHARS = 60

_STATUS_TEXT = {
    STATUS_DISABLED: "disabled",
    STATUS_INVALID: "invalid schedule",
    STATUS_PAST: "past",
}


def _build_schedule(
    schedule_type: str,
    time_of_day: Optional[str],
    at_datetime: Optional[str],
    interval_seconds: Optional[float],
    weekday: Optional[str],
) -> dict:
    """Assemble the persisted schedule dict from the flat tool arguments.

    Raises ``ScheduleInvalid`` on an unknown ``schedule_type``; missing per-type fields are caught
    later, when the service validates the assembled schedule.
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
    raise ScheduleInvalid(f"schedule_type must be one of once, interval, daily, weekly; got {schedule_type!r}")


def _flatten_schedule(schedule: dict) -> dict:
    """The flat ``_build_schedule`` arguments equivalent to a persisted schedule dict.

    This is what lets ``update_scheduled_task`` accept one field: the arguments the caller omits are
    re-derived from the record instead of being dropped, so changing a weekly task's time keeps its day.
    """
    kind = schedule.get("type")
    return {
        "schedule_type": kind,
        "time_of_day": schedule.get("at") if kind in ("daily", "weekly") else None,
        "at_datetime": schedule.get("at") if kind == "once" else None,
        "interval_seconds": schedule.get("seconds") if kind == "interval" else None,
        "weekday": schedule.get("day") if kind == "weekly" else None,
    }


def _handle(record: dict) -> str:
    """How a task is named back to the model and the user: id plus its optional name."""
    return f"{record['id']} ({record.get('name') or 'unnamed'})"


def _unknown(id_or_name: str) -> str:
    return f"No scheduled task matches {id_or_name!r}."


def _next_fire_text(status: str, seconds: Optional[float]) -> str:
    """The countdown column, from a service status: ``~Ns`` when there is one, else why there isn't."""
    return _STATUS_TEXT.get(status, f"~{int(seconds or 0)}s")


def make_scheduling_tools(tasks: TaskService) -> list[Callable]:
    """Build the schedule/read/edit/cancel agent tools over a live :class:`TaskService`."""

    @tool
    async def schedule_task(
        prompt: str,
        schedule_type: Literal["once", "interval", "daily", "weekly"],
        time_of_day: Optional[str] = None,
        at_datetime: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        weekday: Optional[str] = None,
        name: Optional[str] = None,
        target: Literal["active", "new", "task", "latest"] = "active",
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
                "new" runs each firing in its own fresh conversation and keeps them all. "latest" also
                runs in a fresh conversation but deletes the one before it, so the task keeps only its
                most recent run -- use it for a task that fires often and whose old runs are noise.
                "task" gives the task one dedicated conversation, created on the first firing and
                reused on every later firing so it builds on its own history.
        """
        try:
            schedule = _build_schedule(schedule_type, time_of_day, at_datetime, interval_seconds, weekday)
            record, delay = tasks.create(prompt, schedule, name=name, target=target)
        except ScheduleInvalid as exc:
            return f"Invalid schedule: {exc}"
        except SchedulePast:
            return "That time is in the past; choose a future time."
        except DuplicateName as exc:
            return f"A task named {exc.name!r} already exists; cancel it first or use a different name."
        return f"Scheduled task {_handle(record)}; first run in ~{int(delay)}s."

    @tool
    async def list_scheduled_tasks() -> str:
        """List the scheduled tasks: id, name, schedule, next fire, and the start of each prompt.

        Prompts are shown as previews. Use ``get_scheduled_task`` to read one in full.
        """
        items = tasks.list()
        if not items:
            return "No scheduled tasks."
        lines = []
        truncated_any = False
        for item in items:
            prompt = item["prompt"]
            preview = prompt[:PROMPT_PREVIEW_CHARS]
            if len(prompt) > PROMPT_PREVIEW_CHARS:
                # Mark the cut explicitly: an unmarked preview reads as the whole prompt, and a model
                # asked to edit the task then rewrites it from scratch instead of fetching the original.
                preview += "..."
                truncated_any = True
            lines.append(
                f"- {item['id']} [{item['name'] or 'unnamed'}] {item['schedule']} "
                f"next {_next_fire_text(item['status'], item['next_fire_seconds'])} "
                f"target={item['target']}: {preview}"
            )
        if truncated_any:
            lines.append("(prompts truncated; call get_scheduled_task for the full text before editing one)")
        return "\n".join(lines)

    @tool
    async def get_scheduled_task(id_or_name: str) -> str:
        """Show one scheduled task in full, including its complete prompt, by id or name.

        Read a task with this before editing it, so ``update_scheduled_task`` can revise the existing
        prompt rather than replace it with a guess.
        """
        try:
            record = tasks.get(id_or_name)
        except TaskNotFound:
            return _unknown(id_or_name)
        status, seconds = tasks.next_firing(record)
        lines = [
            f"id: {record['id']}",
            f"name: {record.get('name') or 'unnamed'}",
            f"enabled: {record.get('enabled', True)}",
            f"schedule: {record['schedule']}",
            f"next fire: {_next_fire_text(status, seconds)}",
            f"target: {record.get('target') or 'active'}",
            f"created_at: {record.get('created_at', 'unknown')}",
            f"prompt: {record['prompt']}",
        ]
        if record.get("session_id"):
            lines.insert(-1, f"conversation: {record['session_id']}")
        return "\n".join(lines)

    @tool
    async def update_scheduled_task(
        id_or_name: str,
        prompt: Optional[str] = None,
        schedule_type: Optional[Literal["once", "interval", "daily", "weekly"]] = None,
        time_of_day: Optional[str] = None,
        at_datetime: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        weekday: Optional[str] = None,
        name: Optional[str] = None,
        target: Optional[Literal["active", "new", "task", "latest"]] = None,
    ) -> str:
        """Edit an existing scheduled task in place, keeping its id, history, and any fields you omit.

        Call ``get_scheduled_task`` first and pass the full revised text as ``prompt``: this replaces
        the prompt outright, it does not append to it. Omitted schedule fields keep their current
        values, so changing a weekly task's ``time_of_day`` keeps its ``weekday``. The task is re-armed
        only when the schedule actually changes, so editing a prompt never resets an interval countdown.

        Args:
            id_or_name: The task to edit.
            prompt: The complete new instruction, replacing the old one.
            schedule_type: One of "once", "interval", "daily", or "weekly".
            time_of_day: For "daily" or "weekly", a 24-hour "HH:MM".
            at_datetime: For "once", an ISO-8601 local datetime.
            interval_seconds: For "interval", the number of seconds between runs (>= 1).
            weekday: For "weekly", one of mon/tue/wed/thu/fri/sat/sun.
            name: A new unique handle for the task.
            target: Where each firing runs; see ``schedule_task``.
        """
        schedule_args = {
            "schedule_type": schedule_type,
            "time_of_day": time_of_day,
            "at_datetime": at_datetime,
            "interval_seconds": interval_seconds,
            "weekday": weekday,
        }
        try:
            # Merging here, not in the service, keeps the flat arguments in the layer whose schema
            # defines them. It costs a read before the write: harmless under Kokua's one-process,
            # one-user rule, where nothing else can edit the record in between.
            schedule = None
            if any(value is not None for value in schedule_args.values()):
                current = _flatten_schedule(tasks.get(id_or_name)["schedule"])
                merged = {key: (value if value is not None else current[key]) for key, value in schedule_args.items()}
                schedule = _build_schedule(**merged)
            record, changed = tasks.update(id_or_name, prompt=prompt, schedule=schedule, name=name, target=target)
        except TaskNotFound:
            return _unknown(id_or_name)
        except ScheduleInvalid as exc:
            return f"Invalid schedule: {exc}"
        except SchedulePast:
            return "That time is in the past; choose a future time."
        except DuplicateName as exc:
            return f"A task named {exc.name!r} already exists; choose a different name."
        except InvalidTarget as exc:
            return f"target must be one of active, new, task, latest; got {exc.target!r}."

        if not changed:
            return f"Nothing to update on scheduled task {_handle(record)}."
        summary = f"Updated {', '.join(changed)} on scheduled task {_handle(record)}."
        if not record.get("enabled", True):
            return f"{summary} It stays disabled; enable it to resume firing."
        return summary

    @tool
    async def cancel_scheduled_task(id_or_name: str) -> str:
        """Cancel a scheduled task by its id or name."""
        try:
            record = tasks.cancel(id_or_name)
        except TaskNotFound:
            return _unknown(id_or_name)
        return f"Cancelled scheduled task {_handle(record)}."

    def _set_enabled(id_or_name: str, enabled: bool) -> str:
        """Render a set_enabled outcome. Shared by the two tools below, which stay separate because the
        model needs two names and two descriptions to choose between."""
        try:
            result = tasks.set_enabled(id_or_name, enabled)
        except TaskNotFound:
            return _unknown(id_or_name)
        handle = _handle(result.record)
        if not result.changed:
            return f"Scheduled task {handle} is already {'enabled' if enabled else 'disabled'}."
        if not enabled:
            return f"Disabled scheduled task {handle}."
        if not result.armed:  # past-due one-shot: flag flipped, but nothing to schedule
            return f"Enabled scheduled task {handle}, but its scheduled time is in the past, so it will not fire."
        return f"Enabled scheduled task {handle}."

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
        remembers) the task's dedicated conversation; a ``target="latest"`` run replaces the previous
        run's conversation, deleting it once this one finishes. The task's output arrives as a separate message
        shortly after, not as this tool's return value. Works on a disabled task too.
        """
        try:
            record = tasks.run_now(id_or_name)
        except TaskNotFound:
            return _unknown(id_or_name)
        handle = _handle(record)
        suffix = " in a new conversation" if (record.get("target") or "active") in ("new", "task", "latest") else ""
        note = " (note: this task is disabled)" if not record.get("enabled", True) else ""
        return f"Running task {handle} now; its output will appear shortly{suffix}.{note}"

    return [
        schedule_task,
        list_scheduled_tasks,
        get_scheduled_task,
        update_scheduled_task,
        cancel_scheduled_task,
        disable_scheduled_task,
        enable_scheduled_task,
        run_scheduled_task,
    ]


TOOLSET = Toolset(
    name="scheduling",
    description="Schedule, list, edit, and cancel recurring or one-off proactive tasks.",
    build=lambda ctx: make_scheduling_tools(ctx.state.tasks),
    cross_cutting=True,
)
