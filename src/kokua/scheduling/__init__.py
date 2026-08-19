"""Durable scheduled tasks: recurrence math, a JSON registry, and the task lifecycle over both.

The agent tools that drive this live in ``kokua.toolsets.scheduling``; nothing here formats a sentence.
"""

from .recurrence import WEEKDAYS, next_fire
from .registry import add, find, load, remove
from .tasks import (
    DuplicateName,
    EnabledResult,
    InvalidRetention,
    ScheduleInvalid,
    SchedulePast,
    StopResult,
    TaskError,
    TaskNotFound,
    TaskService,
)

__all__ = [
    "next_fire",
    "WEEKDAYS",
    "load",
    "add",
    "remove",
    "find",
    "TaskService",
    "EnabledResult",
    "StopResult",
    "TaskError",
    "TaskNotFound",
    "ScheduleInvalid",
    "SchedulePast",
    "DuplicateName",
    "InvalidRetention",
]
