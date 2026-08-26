"""A conversation as Markdown a person can read and judge.

The export exists because watching a run in the browser only works while the run is on screen.
This is the artifact you keep: what was said, what was reasoned, what was called and what came
back, and the model, effort, tokens, and timing behind each turn, in a file you can diff against
another run or paste into a review.

The reader is a person, so every choice here resolves toward readability. Two rules are
correctness rather than taste, and both are about not claiming more than was recorded: a figure
nobody reported prints as "not reported" and never as zero, and a payload that was cut says so.
The third, fence escalation, is in ``_fenced``.

Pure: it takes a ``Session`` and returns a string. Both front ends and the CLI call it, which is
why it lives here rather than under ``channels/`` or ``frontends/``, and why it imports neither.

This module renders the header and the per-turn structure: the user's words, the model's answer,
its reasoning (both the plain ``thinking`` field and a verbose trace's ``reasoning`` segments),
and the model and effort recorded for the turn. Tool calls, sub-agent cards, and failure notices
are a later task's to render; until then, each of those item types gets a one-line placeholder
here rather than disappearing, so their absence is something a reader (and a later task's tests)
can notice instead of something they have to take on faith.
"""

from __future__ import annotations

from typing import Optional

from aimu.sessions import Session

from kokua.core.messages import derive_title
from kokua.core.transcripts import replay_items, short_time

# Item types a later task owns: rendering them here would collide with that task's own rendering.
# Each still gets a placeholder line (see ``_render_item``) so its absence is visible rather than silent.
_DEFERRED_TYPES = frozenset({"tool", "subagent", "notice"})


def render_markdown(session: Session, *, max_payload_chars: Optional[int] = 4000) -> str:
    """The conversation as a Markdown document: a header, then one section per turn.

    ``max_payload_chars`` is accepted here because it is part of this function's settled shape
    across the whole export (a later task uses it to bound a tool payload's rendered size); this
    task renders no payloads yet, so the argument is unused until then.
    """
    lines = _render_header(session)
    if not session.messages:
        lines.append("")
        lines.append("_This conversation has no messages._")
        return "\n".join(lines) + "\n"

    metadata = session.metadata
    items = replay_items(
        session.messages,
        subagent=metadata.get("subagent"),
        trace=metadata.get("trace"),
        failure=metadata.get("failure"),
    )
    lines.extend(_render_body(items, metadata))
    return "\n".join(lines) + "\n"


def _render_header(session: Session) -> list[str]:
    """Title, id, and the times a reader needs before reading a single turn.

    The title falls back from what the conversation was named, to what its first user message
    said, to a generic heading, in that order, so every export gets exactly one heading whether or
    not the conversation was ever titled. ``task_id`` is included only when present: it is what
    nests a run under the scheduled task that started it, and the field a reader comparing several
    runs of the same task would filter on.
    """
    title = session.metadata.get("title") or derive_title(session.messages) or "Untitled conversation"
    lines = [f"# {title}", "", f"- Conversation: `{session.key}`"]
    created = short_time(session.metadata.get("created_at"))
    if created:
        lines.append(f"- Created: {created}")
    updated = short_time(session.metadata.get("updated_at"))
    if updated:
        lines.append(f"- Updated: {updated}")
    task_id = session.metadata.get("task_id")
    if task_id:
        lines.append(f"- Task: {task_id}")
    return lines


def _when_suffix(item: dict) -> str:
    """`` (YYYY-MM-DD HH:MM)`` for an item carrying a timestamp, or ``""`` for one that doesn't.

    A message persisted before AIMU's inert timestamping shipped has no ``ts``, and inventing one
    would make the export claim a time that was never recorded.
    """
    when = short_time(item.get("ts"))
    return f" ({when})" if when else ""


def _format_effort(value) -> str:
    """The recorded reasoning effort as a word: ``False`` is "off", not a falsy no-op."""
    if value is False:
        return "off"
    if value is True:
        return "on"
    return str(value)


def _turn_meta_line(message_index: Optional[int], metadata: dict) -> Optional[str]:
    """The model and effort recorded for one turn, or ``None`` when neither was recorded.

    Looked up by ``message_index``, the user message's position in ``session.messages``, because
    that is the key ``record_turn_provenance`` wrote the turn's model and effort under, not the
    turn's own display number. A conversation outlives the config that started it, so two turns of
    one conversation can have been answered by different models at different efforts.
    """
    if message_index is None:
        return None
    key = str(message_index)
    model = metadata.get("model", {}).get(key)
    thinking = metadata.get("thinking", {}).get(key)
    if model is None and thinking is None:
        return None
    parts = []
    if model is not None:
        parts.append(f"model {model}")
    if thinking is not None:
        parts.append(f"effort {_format_effort(thinking)}")
    return "_" + ", ".join(parts) + "_"


def _render_item(item: dict) -> list[str]:
    """One replay item as Markdown lines, dispatched on its ``type``.

    Every type ``replay_items`` can emit is named here, even the ones this task does not render in
    full: a type this function doesn't recognize at all (grown after this was written) gets the
    same visible placeholder as a deliberately deferred one, rather than vanishing from the export
    with nothing to show it was ever there.
    """
    item_type = item["type"]
    if item_type == "message":
        prefix = "**Assistant (unprompted):**" if item.get("proactive") else "**Assistant:**"
        return [f"{prefix} {item['text']}"]
    if item_type == "thinking":
        return [f"**Thinking:** {item['text']}"]
    if item_type == "reasoning":
        return [f"**Reasoning:** {item['text']}"]
    if item_type == "phase":
        label = item.get("label") or "Phase"
        detail = item.get("detail")
        return [f"**{label}**" + (f" ({detail})" if detail else "")]
    if item_type == "image":
        url = item.get("url", "")
        return [f"_[image: {url}]_" if url else "_[image]_"]
    if item_type == "loop":
        return [f"_[continued: {item['text']}]_"]
    if item_type in _DEFERRED_TYPES:
        return [f"_({item_type} not yet rendered here)_"]
    return [f"_(unrendered item type: {item_type})_"]


def _render_body(items: list[dict], metadata: dict) -> list[str]:
    """Every item, one per turn heading at a ``user`` item and its content after."""
    lines: list[str] = []
    turn_number = 0
    for item in items:
        lines.append("")
        if item["type"] == "user":
            turn_number += 1
            lines.append(f"## Turn {turn_number}{_when_suffix(item)}")
            meta_line = _turn_meta_line(item.get("message_index"), metadata)
            if meta_line:
                lines.append("")
                lines.append(meta_line)
            lines.append("")
            lines.append(f"**User:** {item['text']}")
        else:
            lines.extend(_render_item(item))
    return lines
