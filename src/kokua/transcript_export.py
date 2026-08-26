"""A conversation as Markdown a person can read and judge.

The export exists because watching a run in the browser only works while the run is on screen.
This is the artifact you keep: what was said, what was reasoned, what was called and what came
back, and the model, effort, tokens, and timing behind each turn, in a file you can diff against
another run or paste into a review.

The reader is a person, so every choice here resolves toward readability. One rule is correctness
rather than taste: nothing recorded is invented, so a turn's model or effort is left out entirely
when it was never recorded, rather than shown as a blank or a made-up default. A tool call's
arguments and result are arbitrary text a model or a tool produced, which means two things have to
be handled rather than assumed away: the payload may itself contain a Markdown fence (code is a
common tool result), and the payload may be large enough to bury the turn being judged under it.
Both are handled here, not deferred: a fenced block always opens with a run of backticks longer
than the longest one inside its own payload, and a payload longer than ``max_payload_chars`` is
cut with a note saying how much was removed, rather than either breaking the rest of the document
or silently showing an incomplete result as if it were complete.

Pure: it takes a ``Session`` and returns a string. Both front ends and the CLI call it, which is
why it lives here rather than under ``channels/`` or ``frontends/``, and why it imports neither.

This module renders the header and the per-turn structure: the user's words, the model's answer,
its reasoning (both the plain ``thinking`` field and a verbose trace's ``reasoning`` segments),
the model and effort recorded for the turn, and each tool call's name, arguments, and result.
Sub-agent cards and failure notices are a later task's to render; until then, each of those item
types gets a one-line placeholder here rather than disappearing, so their absence is something a
reader (and a later task's tests) can notice instead of something they have to take on faith.
"""

from __future__ import annotations

from typing import Optional

from aimu.sessions import Session

from kokua.core.messages import derive_title
from kokua.core.transcripts import replay_items, short_time

# Item types a later task owns: rendering them here would collide with that task's own rendering.
# Each still gets a placeholder line (see ``_render_item``) so its absence is visible rather than silent.
_DEFERRED_TYPES = frozenset({"subagent", "notice"})

# The rendered size of a tool call's arguments or result, past which the export cuts the payload
# rather than let one large call bury the turn being judged. Read by later tasks bounding a
# sub-agent transcript's or notice's own payload the same way, which is why it is a module constant
# rather than a literal inline here.
DEFAULT_MAX_PAYLOAD_CHARS = 4000

# The shortest fence Markdown accepts. Anything longer is escalation past the content's own runs.
_MIN_FENCE = 3


def _fenced(payload: str, language: str = "") -> str:
    """``payload`` in a fenced block whose fence is longer than any backtick run inside it.

    Message and tool text routinely contains fenced code, and a block opened with three backticks
    around a payload containing three ends at the payload's fence rather than at ours: everything
    after it renders as code, and the rest of the export is unreadable. Markdown's rule is that a
    fence closes only on a run at least as long as the one that opened it, so opening with one
    longer than the longest run inside is the fix.

    This is invisible to any test whose fixture holds no code, which is why there is a test that
    holds some.
    """
    longest = 0
    run = 0
    for char in payload:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(_MIN_FENCE, longest + 1)
    return f"{fence}{language}\n{payload}\n{fence}"


def _capped(payload: str, limit: Optional[int]) -> str:
    """``payload``, cut to ``limit`` with a note saying how much is missing.

    The note is the point. A silent cut reads as a complete record of a short result, which is
    the one thing an export must never do: the reader cannot tell that the evidence they are
    judging was abridged.
    """
    if limit is None or len(payload) <= limit:
        return payload
    return f"{payload[:limit]}\n... [truncated, {len(payload)} chars total]"


def render_markdown(session: Session, *, max_payload_chars: Optional[int] = DEFAULT_MAX_PAYLOAD_CHARS) -> str:
    """The conversation as a Markdown document: a header, then one section per turn.

    ``max_payload_chars`` bounds how much of a tool call's arguments or result is shown before
    it is cut with a note (see :func:`_capped`); pass ``None`` to lift the cap entirely.
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
    lines.extend(_render_body(items, metadata, max_payload_chars))
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


def _render_tool(item: dict, max_payload_chars: Optional[int]) -> list[str]:
    """A tool call's name, arguments, and result, each payload capped and fenced.

    The arguments block gets a ``json`` language hint because the model always emits that shape;
    the result's is left bare, since a tool can return anything from plain text to another
    language's source. A ``response`` of ``None`` means no result was recorded (a turn cancelled
    mid-call), which is worth showing as its own line rather than as an empty, easily-missed block.
    """
    lines = [f"**Tool call: `{item['name']}`**", ""]
    arguments = item.get("arguments")
    if arguments:
        lines.append(_fenced(_capped(arguments, max_payload_chars), "json"))
    response = item.get("response")
    lines.append("")
    if response is None:
        lines.append("_(no result recorded)_")
    else:
        lines.append(_fenced(_capped(response, max_payload_chars)))
    return lines


def _render_item(item: dict, max_payload_chars: Optional[int]) -> list[str]:
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
    if item_type == "tool":
        return _render_tool(item, max_payload_chars)
    if item_type in _DEFERRED_TYPES:
        return [f"_({item_type} not yet rendered here)_"]
    return [f"_(unrendered item type: {item_type})_"]


def _render_body(items: list[dict], metadata: dict, max_payload_chars: Optional[int]) -> list[str]:
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
            lines.extend(_render_item(item, max_payload_chars))
    return lines
