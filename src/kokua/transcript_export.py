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
its reasoning (both the plain ``thinking`` field and a verbose trace's ``reasoning`` segments), the
model and effort recorded for the turn, each tool call's name, arguments, and result, what the turn
and any spawned sub-agent cost, and why a turn stopped when it stopped short.

Cost follows the same correctness rule: a figure nobody reported prints as "not reported", never as
an invented zero, since a stored absence rendered as zero is a false claim about what a run cost.
The same principle covers delegation: a turn that spawned a sub-agent but whose usage record carries
no per-agent breakdown (``by_agent``) says so explicitly ("delegation ... not counted"), rather than
showing the entry agent's own total as if it were the whole run's, which would make a heavily
delegating turn read as cheap.
"""

from __future__ import annotations

from typing import Optional

from aimu.sessions import Session

from kokua.core.messages import derive_title
from kokua.core.transcripts import replay_items, short_time

# The rendered size of a tool call's arguments or result, a sub-agent's own text, or a failure
# notice, past which the export cuts the payload rather than let one large piece of text bury the
# turn being judged.
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


# What each injected round is called in an export. The two say opposite things to the model (keep
# working, or stop and answer from what you have), and an item stored before they were told apart
# carries no reason and reads as the commoner of them. The wording is a phrase here and a single
# kind word in the page's row ("tool-use limit"), because this one stands alone inside brackets in
# prose while that one labels a foldable row a reader can open.
_LOOP_LABELS = {"continuation": "continued", "final_answer": "tool-use limit reached"}


def _loop_line(item: dict) -> str:
    """The injected round as one line of prose for the export."""
    label = _LOOP_LABELS.get(item.get("reason") or "continuation", "continued")
    return f"_[{label}: {item.get('text', '')}]_"


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
    usage = session.metadata.get("usage") or {}
    if usage:
        lines.append(f"- Totals: {_format_usage(_sum_usage(list(usage.values())))}")
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


def _reported_calls(record: dict) -> int:
    """How many of ``record``'s calls reported token usage.

    Prefers the stored ``reported_calls`` (what ``TurnMetrics`` actually writes: set whenever any
    call in the record reported a figure). A record missing that key but carrying ``input_tokens``
    or ``output_tokens`` anyway (a hand-built one, or a caller summing raw provider usage) is read
    as fully reported rather than as reporting nothing, since the tokens are proof that something did.
    """
    if "reported_calls" in record:
        return record["reported_calls"]
    if record.get("input_tokens") is not None or record.get("output_tokens") is not None:
        return record.get("calls", 0)
    return 0


def _format_tokens(record: dict) -> str:
    """``record``'s input/output token figures as prose.

    Never zero: a key absent from the record means no provider reported it, and printing "not
    reported" instead of "0" is the one correctness rule this whole feature exists to enforce. When
    fewer calls reported than ran, the figure is qualified rather than presented as a complete sum.
    """
    input_tokens = record.get("input_tokens")
    output_tokens = record.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return "tokens not reported"
    in_text = f"{input_tokens:,}" if input_tokens is not None else "not reported"
    out_text = f"{output_tokens:,}" if output_tokens is not None else "not reported"
    text = f"{in_text} in / {out_text} out tokens"
    calls = record.get("calls", 0)
    reported_calls = _reported_calls(record)
    if reported_calls and reported_calls < calls:
        text += f" (reported by {reported_calls} of {calls} calls)"
    return text


def _format_usage(record: dict) -> str:
    """One usage record (a turn's, or the conversation's summed total) as one line of prose."""
    calls = record.get("calls", 0)
    plural = "" if calls == 1 else "s"
    parts = [
        f"{calls} model call{plural}",
        _format_tokens(record),
        f"{record.get('model_seconds', 0.0)}s model time ({record.get('wall_seconds', 0.0)}s wall)",
    ]
    return ", ".join(parts)


def _sum_usage(records: list[dict]) -> dict:
    """The conversation-wide total of every turn's usage record, in the same shape ``_format_usage``
    already knows how to render.

    Mirrors ``TurnMetrics._totals``'s own rule one level up: a token figure is summed only from the
    records that reported one. ``reported_calls`` is summed too and carried into the total, which is
    what lets ``_format_tokens`` qualify the header's figure the same way it qualifies a turn's own:
    without it, a conversation of five unmeasured calls and one measured one would sum to "six calls,
    the one measured call's tokens" with no caveat at all, stating a partial figure as complete.
    """
    total = {
        "calls": sum(record.get("calls", 0) for record in records),
        "model_seconds": round(sum(record.get("model_seconds", 0.0) for record in records), 1),
        "wall_seconds": round(sum(record.get("wall_seconds", 0.0) for record in records), 1),
    }
    reported_calls = sum(_reported_calls(record) for record in records)
    if reported_calls:
        total["reported_calls"] = reported_calls
        input_figures = [record["input_tokens"] for record in records if record.get("input_tokens") is not None]
        output_figures = [record["output_tokens"] for record in records if record.get("output_tokens") is not None]
        if input_figures:
            total["input_tokens"] = sum(input_figures)
        if output_figures:
            total["output_tokens"] = sum(output_figures)
    return total


def _render_by_agent(by_agent: dict) -> list[str]:
    """One bullet per agent that contributed to a turn, sorted by name for a stable order.

    Only ever called when ``by_agent`` is present, which ``TurnMetrics`` sets only once more than
    one agent contributed: this is exactly the delegated-work cost a heavily delegating turn would
    otherwise hide behind the entry agent's own total.
    """
    lines = []
    for name in sorted(by_agent):
        record = by_agent[name]
        calls = record.get("calls", 0)
        plural = "" if calls == 1 else "s"
        lines.append(f"- {name}: {calls} call{plural}, {_format_tokens(record)}")
    return lines


def _turn_cost_blocks(message_index: Optional[int], metadata: dict) -> list[list[str]]:
    """What a turn cost, as paragraph-sized blocks of lines (each rendered with a blank line before
    it, but no blank line splitting it internally).

    Two things can appear, independently: the turn's own measured cost (with a per-agent breakdown
    when more than one agent contributed), and a note that delegation happened but was not measured
    at all (a turn can spawn a sub-agent under an AIMU predating the seam that attributes calls to
    it), and presenting that turn's entry-agent-only total as the whole run's cost would read as a
    cheap delegating run, which is the opposite of true. A spawn is identified the same way
    ``core/transcripts.py`` tells one apart from a workflow reviewer's verdict card: a "task" key on its
    create event.
    """
    if message_index is None:
        return []
    key = str(message_index)
    blocks: list[list[str]] = []
    record = metadata.get("usage", {}).get(key)
    if record is not None:
        block = ["_" + _format_usage(record) + "_"]
        by_agent = record.get("by_agent")
        if by_agent:
            block.extend(_render_by_agent(by_agent))
        blocks.append(block)
    spawned = any("task" in event for event in metadata.get("subagent", {}).get(key, []))
    if spawned and not (record or {}).get("by_agent"):
        blocks.append(["_Delegation happened this turn, but it was not counted separately._"])
    return blocks


def _render_subagent(events: list[dict], max_payload_chars: Optional[int]) -> list[str]:
    """One spawn's whole lifecycle, grouped by its caller into a single card.

    ``events`` is every event recorded for one spawn id, in emission order (see
    ``core/subagents.py``'s ``SubagentReporter``). The first is always the create event, carrying
    the role, the task, and, when configured, the model and reasoning effort; the rest are whatever
    it produced (reasoning, a tool call, generated text) and the terminal status that closed it,
    which may itself carry a final chunk when nothing streamed live. A sub-agent's reasoning and
    generated text are arbitrary model output exactly like a tool result, so they get the same
    fencing and capping: an answer containing its own code fence must not break the rest of the
    document, and a long one must not bury the turn that spawned it.

    Only ever called on a spawn's events (grouped by id in ``_render_subagent_run``); a workflow
    reviewer's id-less verdict round is a different shape entirely and goes through
    :func:`_render_verdict` instead.
    """
    create = events[0]
    role = create.get("role", "subagent")
    task = create.get("task", "")
    header = f"**Sub-agent ({role}):**" + (f" {task}" if task else "")
    lines = [header]
    details = []
    model = create.get("model")
    if model:
        details.append(f"model {model}")
    if "thinking" in create:
        details.append(f"effort {_format_effort(create['thinking'])}")
    if details:
        lines.append("")
        lines.append("_" + ", ".join(details) + "_")
    for event in events[1:]:
        append = event.get("append")
        if append is None:
            continue
        kind = append.get("kind")
        lines.append("")
        if kind == "tool":
            lines.extend(_render_tool(append, max_payload_chars))
        elif kind == "reasoning":
            lines.append("**Reasoning:**")
            lines.append("")
            lines.append(_fenced(_capped(append.get("text", ""), max_payload_chars)))
        elif kind == "answer":
            lines.append(_fenced(_capped(append.get("text", ""), max_payload_chars)))
        elif kind == "error":
            lines.append("**Error:**")
            lines.append("")
            lines.append(_fenced(_capped(append.get("text", ""), max_payload_chars)))
        elif kind == "loop":
            lines.append(_loop_line(append))
    status = events[-1].get("status")
    if status:
        lines.append("")
        lines.append(f"_status: {status}_")
    return lines


def _render_verdict(event: dict, max_payload_chars: Optional[int]) -> list[str]:
    """One workflow reviewer's verdict round as its own card.

    ``planning/runner.py``'s ``_verdict_event`` persists a round as one complete, id-less record
    (``role``, ``status``, ``issues``, ``round``) rather than a lifecycle to reassemble: there is no
    create-then-chunks-then-terminal sequence here, which is why ``_render_subagent_run`` hands this
    function exactly one event at a time instead of grouping several together. ``issues`` is a list
    (empty on an approved round), rendered as its own bullet per item; each item is capped like any
    other payload, since a reviewer's issue text is free-form and could in principle be large.
    """
    role = event.get("role", "reviewer")
    round_ = event.get("round")
    heading = f"**Reviewer ({role})" + (f", round {round_ + 1}" if isinstance(round_, int) else "") + ":**"
    lines = [heading]
    issues = event.get("issues") or []
    if issues:
        lines.append("")
        for issue in issues:
            lines.append(f"- {_capped(str(issue), max_payload_chars)}")
    status = event.get("status")
    if status:
        lines.append("")
        lines.append(f"_status: {status}_")
    return lines


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

    ``subagent`` items are handled by ``_render_body`` before an item ever reaches here, since a
    spawn's several lifecycle events have to be grouped by id into one card rather than rendered
    line by line. Every other type ``replay_items`` can emit is named here; a type this function
    doesn't recognize at all (grown after this was written) gets a visible placeholder rather than
    vanishing from the export with nothing to show it was ever there.
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
        return [_loop_line(item)]
    if item_type == "tool":
        return _render_tool(item, max_payload_chars)
    if item_type == "notice":
        # A blockquote, so the reason a turn stopped reads as an aside on whatever it produced
        # before stopping, not as more of that output.
        text = _capped(item.get("text", ""), max_payload_chars)
        return [f"> {line}" for line in text.splitlines()] or ["> "]
    return [f"_(unrendered item type: {item_type})_"]


def _render_subagent_run(items: list[dict], start: int, max_payload_chars: Optional[int]) -> tuple[list[str], int]:
    """The contiguous run of ``subagent`` items starting at ``start``, as one card per spawn (or
    per reviewer verdict round).

    ``replay_items`` emits a turn's whole set of spawn events together, right after its user item
    and before any of the turn's own assistant output, so they always arrive as one contiguous run;
    but several ids can interleave within that run (concurrent spawns append to one shared list in
    whatever order they actually produced something), so a spawn's events are grouped by ``id``
    rather than by position.

    A workflow reviewer's verdict round has no ``id`` at all (``planning/runner.py``'s
    ``_verdict_event`` persists "the persisted (id-less) form of a reviewer verdict", by its own
    docstring): every such event is already a complete, standalone record, never a fragment of a
    lifecycle to reassemble. Grouping those by their shared, absent id would merge unrelated rounds
    (even rounds from two different reviewers) into one mislabeled card and drop every round but the
    last, so each id-less event becomes its own card instead of joining any group.

    Returns the rendered lines and the index of the first item past the run.
    """
    groups: dict[str, list[dict]] = {}
    order: list[list[dict]] = []
    index = start
    while index < len(items) and items[index]["type"] == "subagent":
        event = items[index]
        spawn_id = event.get("id")
        if spawn_id is None:
            order.append([event])
        else:
            group = groups.get(spawn_id)
            if group is None:
                group = []
                groups[spawn_id] = group
                order.append(group)
            group.append(event)
        index += 1
    lines: list[str] = []
    for group in order:
        lines.append("")
        if group[0].get("id") is None:
            lines.extend(_render_verdict(group[0], max_payload_chars))
        else:
            lines.extend(_render_subagent(group, max_payload_chars))
    return lines, index


def _render_body(items: list[dict], metadata: dict, max_payload_chars: Optional[int]) -> list[str]:
    """Every item, one per turn heading at the first item carrying a ``message_index`` and its content
    after.

    A turn heading opens on ``message_index`` rather than on ``item["type"] == "user"``: a user message
    sent with an image and no text yields no ``"user"`` item at all (see ``replay_items``), only an
    image item carrying the index instead, and that item still has to open the turn or its heading, its
    model/effort line, and its cost block all go missing while the header total still counts it. A
    user's text and image items can share one index, so a new heading opens only the first time a given
    index is seen (tracked in ``open_index``), not on every item carrying one.
    """
    lines: list[str] = []
    turn_number = 0
    open_index: object = object()  # sentinel: no real message_index (an int, or None) ever equals it
    index = 0
    while index < len(items):
        item = items[index]
        if item["type"] == "subagent":
            card_lines, index = _render_subagent_run(items, index, max_payload_chars)
            lines.extend(card_lines)
            continue
        lines.append("")
        message_index = item.get("message_index")
        if message_index is not None and message_index != open_index:
            open_index = message_index
            turn_number += 1
            lines.append(f"## Turn {turn_number}{_when_suffix(item)}")
            meta_line = _turn_meta_line(message_index, metadata)
            if meta_line:
                lines.append("")
                lines.append(meta_line)
            for block in _turn_cost_blocks(message_index, metadata):
                lines.append("")
                lines.extend(block)
            lines.append("")
        if item["type"] == "user":
            lines.append(f"**User:** {item['text']}")
        else:
            lines.extend(_render_item(item, max_payload_chars))
        index += 1
    return lines
