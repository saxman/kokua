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

import re
from typing import Callable, Literal, Optional

from aimu.tools import tool

from kokua.config.store import disambiguate_name
from kokua.scheduling.tasks import (
    STATUS_DISABLED,
    STATUS_INVALID,
    STATUS_PAST,
    DuplicateName,
    InvalidRetention,
    ScheduleInvalid,
    SchedulePast,
    TaskNotFound,
    TaskService,
)
from kokua.registry.registry import Setting, Toolset

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


#: How many of a prompt's words a derived name keeps. Enough to tell two tasks apart at a glance in
#: the sidebar and in config.toml, short enough to stay readable as a TOML table header.
_SLUG_WORDS = 5


def _slug(prompt: str) -> str:
    """A readable table name derived from a prompt: its first few words, lowercased and hyphenated.

    Falls back to ``"task"`` for a prompt with no alphanumeric content at all, since a task must have
    a name to be a table key and an empty one is not a name.
    """
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:_SLUG_WORDS]
    return "-".join(words) or "task"


def _derive_name(prompt: str, used: set[str]) -> str:
    """A free name for a task scheduled without one, derived from its prompt.

    Reuses the MCP path's suffixing rather than a second scheme, because both answer the same
    question: the file needs a unique key and the caller supplied none.
    """
    return disambiguate_name(_slug(prompt), used)


def _handle(record: dict) -> str:
    """How a task is named back to the model and the user. The name is the task's identity."""
    return record["name"]


def _unknown(name: str) -> str:
    return f"No scheduled task matches {name!r}."


def _next_fire_text(status: str, seconds: Optional[float]) -> str:
    """The countdown column, from a service status: ``~Ns`` when there is one, else why there isn't."""
    return _STATUS_TEXT.get(status, f"~{int(seconds or 0)}s")


#: How many conversations a task keeps when it names no cap of its own, and the default the
#: ``[scheduling]`` setting ships with. Enough to compare the last few runs without a task that fires
#: every minute filling the sidebar.
DEFAULT_MAX_TASK_CONVERSATIONS = 3


def _keep_text(own: Optional[int], default: Optional[int] = None) -> str:
    """How a task's retention cap reads: its own value, or the default it inherits, marked as such."""
    if own is None and default is None:
        return "default"
    if own is None:
        return f"{default} (default)"
    return "unlimited" if own == 0 else str(own)


def _bad_retention(exc: InvalidRetention) -> str:
    return f"max_conversations must be 0 (unlimited) or more; got {exc.value!r}."


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
        max_conversations: Optional[int] = None,
    ) -> str:
        """Schedule a task that runs an unprompted assistant turn with the given prompt when it is due.

        Args:
            prompt: The instruction to run when the task fires.
            schedule_type: One of "once", "interval", "daily", or "weekly".
            time_of_day: For "daily" or "weekly", a 24-hour "HH:MM", e.g. "20:00".
            at_datetime: For "once", an ISO-8601 local datetime, e.g. "2026-07-16T17:00:00".
            interval_seconds: For "interval", the number of seconds between runs (>= 1).
            weekday: For "weekly", one of mon/tue/wed/thu/fri/sat/sun.
            name: A unique handle for the task; it becomes its table name in config.toml and shows in
                the sidebar. Supply a short kebab-case one. Omitted, a name is derived from the prompt.
            max_conversations: How many of this task's conversations to keep. Every firing runs in its
                own conversation; once there are more than this, the oldest are deleted. 1 means each
                run replaces the one before it, which suits a task that fires often and whose old runs
                are noise. 0 keeps every run forever. Omit it to follow the configured default.
        """
        try:
            schedule = _build_schedule(schedule_type, time_of_day, at_datetime, interval_seconds, weekday)
            handle = (name or "").strip() or _derive_name(prompt, {item["name"] for item in tasks.list()})
            record, delay = tasks.create(prompt, schedule, name=handle, max_conversations=max_conversations)
        except ScheduleInvalid as exc:
            return f"Invalid schedule: {exc}"
        except SchedulePast:
            return "That time is in the past; choose a future time."
        except DuplicateName as exc:
            return f"A task named {exc.name!r} already exists; cancel it first or use a different name."
        except InvalidRetention as exc:
            return _bad_retention(exc)
        return f"Scheduled task {_handle(record)}; first run in ~{int(delay)}s."

    @tool
    async def list_scheduled_tasks() -> str:
        """List the scheduled tasks: name, schedule, next fire, and the start of each prompt.

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
                f"- {item['name']} {item['schedule']} "
                f"next {_next_fire_text(item['status'], item['next_fire_seconds'])} "
                f"keep={_keep_text(item['max_conversations'])}: {preview}"
            )
        if truncated_any:
            lines.append("(prompts truncated; call get_scheduled_task for the full text before editing one)")
        return "\n".join(lines)

    @tool
    async def get_scheduled_task(name: str) -> str:
        """Show one scheduled task in full, including its complete prompt, by name.

        Read a task with this before editing it, so ``update_scheduled_task`` can revise the existing
        prompt rather than replace it with a guess.
        """
        try:
            record = tasks.get(name)
        except TaskNotFound:
            return _unknown(name)
        status, seconds = tasks.next_firing(record)
        lines = [
            f"name: {record['name']}",
            f"enabled: {record.get('enabled', True)}",
            f"schedule: {record['schedule']}",
            f"next fire: {_next_fire_text(status, seconds)}",
            f"keep: {_keep_text(record.get('max_conversations'), tasks.default_max_conversations())}",
            f"created_at: {record.get('created_at', 'unknown')}",
        ]
        if record.get("fired_at"):
            lines.append(f"fired_at: {record['fired_at']}")
        lines.append(f"prompt: {record['prompt']}")
        return "\n".join(lines)

    @tool
    async def update_scheduled_task(
        name: str,
        prompt: Optional[str] = None,
        schedule_type: Optional[Literal["once", "interval", "daily", "weekly"]] = None,
        time_of_day: Optional[str] = None,
        at_datetime: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        weekday: Optional[str] = None,
        new_name: Optional[str] = None,
        max_conversations: Optional[int] = None,
    ) -> str:
        """Edit an existing scheduled task in place, keeping its history and any fields you omit.

        Call ``get_scheduled_task`` first and pass the full revised text as ``prompt``: this replaces
        the prompt outright, it does not append to it. Omitted schedule fields keep their current
        values, so changing a weekly task's ``time_of_day`` keeps its ``weekday``. The task is re-armed
        only when the schedule actually changes, so editing a prompt never resets an interval countdown.

        Args:
            name: The task to edit, by name.
            prompt: The complete new instruction, replacing the old one.
            schedule_type: One of "once", "interval", "daily", or "weekly".
            time_of_day: For "daily" or "weekly", a 24-hour "HH:MM".
            at_datetime: For "once", an ISO-8601 local datetime.
            interval_seconds: For "interval", the number of seconds between runs (>= 1).
            weekday: For "weekly", one of mon/tue/wed/thu/fri/sat/sun.
            new_name: A new unique name for the task. Renaming moves its table in config.toml and
                re-points the conversations it has already run, so its history follows it.
            max_conversations: How many of this task's conversations to keep; see ``schedule_task``.
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
                current = _flatten_schedule(tasks.get(name)["schedule"])
                merged = {key: (value if value is not None else current[key]) for key, value in schedule_args.items()}
                schedule = _build_schedule(**merged)
            record, changed = tasks.update(
                name, prompt=prompt, schedule=schedule, new_name=new_name, max_conversations=max_conversations
            )
        except TaskNotFound:
            return _unknown(name)
        except ScheduleInvalid as exc:
            return f"Invalid schedule: {exc}"
        except SchedulePast:
            return "That time is in the past; choose a future time."
        except DuplicateName as exc:
            return f"A task named {exc.name!r} already exists; choose a different name."
        except InvalidRetention as exc:
            return _bad_retention(exc)

        if not changed:
            return f"Nothing to update on scheduled task {_handle(record)}."
        summary = f"Updated {', '.join(changed)} on scheduled task {_handle(record)}."
        if not record.get("enabled", True):
            return f"{summary} It stays disabled; enable it to resume firing."
        return summary

    @tool
    async def cancel_scheduled_task(name: str) -> str:
        """Cancel a scheduled task by name."""
        try:
            record = tasks.cancel(name)
        except TaskNotFound:
            return _unknown(name)
        return f"Cancelled scheduled task {_handle(record)}."

    def _set_enabled(name: str, enabled: bool) -> str:
        """Render a set_enabled outcome. Shared by the two tools below, which stay separate because the
        model needs two names and two descriptions to choose between."""
        try:
            result = tasks.set_enabled(name, enabled)
        except TaskNotFound:
            return _unknown(name)
        handle = _handle(result.record)
        if not result.changed:
            return f"Scheduled task {handle} is already {'enabled' if enabled else 'disabled'}."
        if not enabled:
            return f"Disabled scheduled task {handle}."
        if not result.armed:  # past-due one-shot: flag flipped, but nothing to schedule
            return f"Enabled scheduled task {handle}, but its scheduled time is in the past, so it will not fire."
        return f"Enabled scheduled task {handle}."

    @tool
    async def disable_scheduled_task(name: str) -> str:
        """Disable a scheduled task by name: it stops firing but stays in the registry.

        Re-enable it later with ``enable_scheduled_task``. Use ``cancel_scheduled_task`` to remove it.
        """
        return _set_enabled(name, False)

    @tool
    async def enable_scheduled_task(name: str) -> str:
        """Re-enable a disabled scheduled task by name so it resumes firing on its schedule."""
        return _set_enabled(name, True)

    @tool
    async def run_scheduled_task(name: str) -> str:
        """Run an existing scheduled task now, without changing its schedule.

        Reproduces exactly what the task's next scheduled firing would do (a conversation of its own,
        its retention cap applied afterwards, and gated tools auto-denied as they would be for an
        unattended firing), so you can verify how the task behaves. The task's output arrives as a
        separate message shortly after, not as this tool's return value. Works on a disabled task too.
        """
        try:
            record = tasks.run_now(name)
        except TaskNotFound:
            return _unknown(name)
        handle = _handle(record)
        note = " (note: this task is disabled)" if not record.get("enabled", True) else ""
        return f"Running task {handle} now; its output will appear shortly in a new conversation.{note}"

    @tool
    async def stop_scheduled_task(name: str) -> str:
        """Stop a scheduled task's run that is happening right now, by name.

        Only affects a run in flight: the task stays on its schedule and will fire again as usual, so
        use ``disable_scheduled_task`` to keep it from coming back. The stopped run keeps whatever it had
        produced so far in its own conversation.
        """
        try:
            result = tasks.stop(name)
        except TaskNotFound:
            return _unknown(name)
        handle = _handle(result.record)
        if result.stopped:
            runs = "1 run" if result.stopped == 1 else f"{result.stopped} runs"
            return f"Stopped {runs} of scheduled task {handle}. Its schedule is unchanged, so it will fire again."
        if result.skipped_self:
            return (
                f"Scheduled task {handle} is the task running this very turn, and a run cannot stop "
                "itself. Finish here instead, or stop this run from outside it."
            )
        return f"Scheduled task {handle} is not running right now, so there was nothing to stop."

    return [
        schedule_task,
        list_scheduled_tasks,
        get_scheduled_task,
        update_scheduled_task,
        cancel_scheduled_task,
        disable_scheduled_task,
        enable_scheduled_task,
        run_scheduled_task,
        stop_scheduled_task,
    ]


#: The ``[scheduling]`` section of config.toml. Hot because ``update_config`` should be able to change
#: it mid-session: ``TaskService`` reads it at fire time rather than caching it, so the next firing
#: follows the new value. A task's own ``[scheduling.task.*]`` table is not hot in this sense: it is
#: locked against ``update_config`` and written through the scheduling tools, which arm the scheduler
#: to match.
SCHEDULING_SETTINGS: tuple[Setting, ...] = (
    Setting("max_task_conversations", int, DEFAULT_MAX_TASK_CONVERSATIONS, hot=True),
)

TOOLSET = Toolset(
    name="scheduling",
    description="Schedule, list, edit, and cancel recurring or one-off proactive tasks.",
    build=lambda ctx: make_scheduling_tools(ctx.state.tasks),
    settings=SCHEDULING_SETTINGS,
    cross_cutting=True,
)
