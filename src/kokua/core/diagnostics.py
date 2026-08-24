"""The `/diag` report: a snapshot of live turn and gate state.

Reads only in-memory state and never awaits the turn gate, so it still answers while a hung turn is
holding it -- which is the case it exists to diagnose. Keep it that way: anything here that could
block would make the command useless exactly when it is needed.
"""

from __future__ import annotations

import io
import time
from typing import Optional

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

    Worth a line because the model is read only at startup and is not a runtime setting, so a running
    session would otherwise not say which one it is. Only agents that declare their own appear after the first:
    listing every agent would repeat the default once per table and bury the exception among them.
    """
    overrides = [f"{name}: {agent.model}" for name, agent in sorted(config.agents.items()) if agent.model]
    return " | ".join([f"- model: {entry_model or 'unresolved'}", *overrides])


def _render_thinking(value) -> str:
    """One reasoning-effort value as a word for a person: ``False`` reads as "off" rather than "False",
    and ``True`` as "on", since neither Python spelling is what the user wrote or wants to read."""
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def _thinking_line(config: AssistantConfig) -> Optional[str]:
    """The reasoning effort in play, or ``None`` when nothing is declared anywhere.

    Worth a line for the same reason the model is: read only at startup, and not a runtime setting.
    Omitted entirely in the common case, where every agent is at AIMU's own default and a line saying so
    would be noise on every ``/diag``. Tested against ``is not None`` throughout, so an agent declaring
    ``thinking = false`` appears rather than being read as undeclared.
    """
    overrides = [
        f"{name}: {_render_thinking(agent.thinking)}"
        for name, agent in sorted(config.agents.items())
        if agent.thinking is not None
    ]
    if config.thinking is None and not overrides:
        return None
    default = _render_thinking(config.thinking) if config.thinking is not None else "unset"
    return " | ".join([f"- thinking: {default}", *overrides])


def _render_generation(parameters: dict) -> str:
    """One generation table as ``key=value`` pairs, sorted so two reports of one config read alike."""
    return ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()))


def _generation_line(config: AssistantConfig) -> Optional[str]:
    """The generation parameters in play, or ``None`` when nothing is declared anywhere.

    Worth a line for the reason the model and the effort are: read only at startup, and not runtime
    settings, so a running session would otherwise not say what it is sampling at. Each agent that
    overrides the default shows only its own keys, not the merged result, because what a table declares
    is what a reader is checking against the file.
    """
    overrides = [
        f"{name}: {_render_generation(agent.generation)}"
        for name, agent in sorted(config.agents.items())
        if agent.generation
    ]
    if not config.generation and not overrides:
        return None
    default = _render_generation(config.generation) if config.generation else "unset"
    return " | ".join([f"- generation: {default}", *overrides])


def diag_report(
    tracker: TurnTracker,
    gate: TurnGate,
    *,
    config: AssistantConfig,
    entry_model: str,
    pending_approval: bool,
    pending_decision: bool,
) -> str:
    """The `/diag` text: the models, reasoning effort, and generation parameters in play, in-flight
    turns, gate depth, pending human decisions, and stuck-turn stacks.

    ``entry_model`` is passed in rather than derived here because the caller already holds it: it is
    what ``build.model_label`` renders, and this module reports what the assistant is running rather
    than working it out a second time. It was once passed for a stronger reason, that with nothing
    declared the answer existed only on the live client; ``AssistantConfig.default_model`` answers it
    now, so this is plumbing rather than the only route.
    """
    turns = tracker.all()
    lines = ["Diagnostics:", _model_line(config, entry_model)]
    thinking = _thinking_line(config)
    if thinking is not None:
        lines.append(thinking)
    generation = _generation_line(config)
    if generation is not None:
        lines.append(generation)
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
