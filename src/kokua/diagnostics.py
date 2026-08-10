"""The `/diag` report: a snapshot of live turn and gate state.

Reads only in-memory state and never awaits the turn gate, so it still answers while a hung turn is
holding it -- which is the case it exists to diagnose. Keep it that way: anything here that could
block would make the command useless exactly when it is needed.
"""

from __future__ import annotations

import io
import time

from .turn_gate import TurnGate
from .turn_registry import TurnTracker


def format_task_stack(task) -> str:
    """Render an asyncio task's current async stack in-process (a sudo-free py-spy). Best-effort:
    returns '' if the task finished or the dump fails."""
    try:
        buffer = io.StringIO()
        task.print_stack(file=buffer)
        return buffer.getvalue().strip()
    except Exception:
        return ""


def diag_report(tracker: TurnTracker, gate: TurnGate, *, pending_approval: bool, pending_plan: bool) -> str:
    """The `/diag` text: in-flight turns, gate depth, pending human decisions, and stuck-turn stacks."""
    turns = tracker.all()
    lines = ["Diagnostics:"]
    if turns:
        lines.append(f"- turn in flight: yes ({len(turns)})")
        for conversation_id, info in turns:
            elapsed = time.monotonic() - info.started
            lines.append(f"  - {conversation_id}: elapsed {elapsed:.1f}s, message: {info.preview!r}")
    else:
        lines.append("- turn in flight: no")
    lines.append(f"- active turns: {gate.active_turns()}")
    lines.append(
        f"- pending approval: {'yes' if pending_approval else 'no'} | pending plan: {'yes' if pending_plan else 'no'}"
    )
    for conversation_id, info in turns:
        if info.handle.done:
            continue
        stack = format_task_stack(info.handle.task)
        if stack:
            lines.append(
                f"\nStuck turn stack for {conversation_id} "
                f"(async only; run `kill -USR1 <pid>` for full thread stacks):\n```\n{stack}\n```"
            )
    return "\n".join(lines)
