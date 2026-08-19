"""The `/diag` report: a snapshot of live turn and gate state.

Reads only in-memory state and never awaits the turn gate, so it still answers while a hung turn is
holding it -- which is the case it exists to diagnose. Keep it that way: anything here that could
block would make the command useless exactly when it is needed.
"""

from __future__ import annotations

import io
import time

from kokua.config.schema import AssistantConfig
from kokua.core.turn_gate import TurnGate
from kokua.core.turn_registry import TurnTracker


def format_task_stack(task) -> str:
    """Render an asyncio task's current async stack in-process (a sudo-free py-spy). Best-effort:
    returns '' if the task finished or the dump fails."""
    try:
        buffer = io.StringIO()
        task.print_stack(file=buffer)
        return buffer.getvalue().strip()
    except Exception:
        return ""


def _model_line(config: AssistantConfig, entry_model: str) -> str:
    """The models this session is running on: the entry agent's, then each worker that overrides it.

    Worth a line because the model is read only at startup and has no panel field, so a running session
    would otherwise not say which one it is. Only agents that declare their own appear after the first:
    listing every agent would repeat the default once per table and bury the exception among them.
    """
    overrides = [f"{name}: {agent.model}" for name, agent in sorted(config.agents.items()) if agent.model]
    return " | ".join([f"- model: {entry_model or 'unresolved'}", *overrides])


def diag_report(
    tracker: TurnTracker,
    gate: TurnGate,
    *,
    config: AssistantConfig,
    entry_model: str,
    pending_approval: bool,
    pending_decision: bool,
) -> str:
    """The `/diag` text: the models in play, in-flight turns, gate depth, pending human decisions, and
    stuck-turn stacks.

    ``entry_model`` is passed in rather than read off ``config`` because with nothing declared the only
    place the answer exists is the live client (see ``build.model_label``), which this module has no
    route to.
    """
    turns = tracker.all()
    lines = ["Diagnostics:", _model_line(config, entry_model)]
    if turns:
        lines.append(f"- turn in flight: yes ({len(turns)})")
        for conversation_id, info in turns:
            elapsed = time.monotonic() - info.started
            lines.append(f"  - {conversation_id}: elapsed {elapsed:.1f}s, message: {info.preview!r}")
    else:
        lines.append("- turn in flight: no")
    lines.append(f"- active turns: {gate.active_turns()}")
    lines.append(
        f"- pending approval: {'yes' if pending_approval else 'no'} | pending decision: "
        f"{'yes' if pending_decision else 'no'}"
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
