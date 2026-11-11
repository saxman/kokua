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

A sub-agent's tool result can be capped a second way, before this module ever sees it:
``core/subagents.py`` spills anything past ``RESPONSE_PREVIEW_CHARS`` to a payload file and stores
only a preview plus a ``response_ref``. That preview is already short enough that ``max_payload_chars``
never re-triggers on it, so a caller relying on the cap-and-note above alone would render a complete
looking result that is actually a quarter of one; an item carrying ``response_ref`` is therefore always
noted regardless of length, and under ``max_payload_chars=None`` (``--full``) the blob is read back
through ``payloads_path`` so the export is whole rather than merely uncapped.

Mostly pure: given a ``Session`` it returns a string, except that a spilled tool response makes it
read one payload file back off disk when the caller asks for the full text and supplies
``payloads_path``. Both front ends and the CLI call it, which is why it lives here rather than under
``channels/`` or ``frontends/``, and why it imports neither of those (``kokua.payloads`` is the
shared, front-end-agnostic module the spill itself already depends on).

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

import json
from pathlib import Path
from typing import Any, Optional

from aimu.sessions import Session

from kokua import payloads
from kokua.core.conversations import turn_numbers
from kokua.core.messages import derive_title, message_text
from kokua.core.transcripts import replay_items, short_time

# The rendered size of a tool call's arguments or result, a sub-agent's own text, or a failure
# notice, past which the export cuts the payload rather than let one large piece of text bury the
# turn being judged.
DEFAULT_MAX_PAYLOAD_CHARS = 4000

# The shortest fence Markdown accepts. Anything longer is escalation past the content's own runs.
_MIN_FENCE = 3


def export_filename(session_key: str, *, full: bool) -> str:
    """The file an export of this conversation goes to, named for its fidelity as well as its id.

    Fidelity is in the name because a full export and a trimmed one are different artifacts that read
    identically. While both went to ``<key>.md``, a later trimmed export replaced a full one in place,
    and nothing in the result announced the loss: a capped payload is marked where it was cut, so a
    file whose caps were never needed and a file whose caps removed the answer look the same to
    whoever opens it next. A trimmed export keeps the bare ``<key>.md`` that
    ``docs/how-to/export-a-conversation.md`` documents, since it is the default the flag opts out of.

    Both callers derive the path from here (the ``export_conversation`` tool and ``kokua export``) so
    the two cannot drift into disagreeing about where one conversation's export lives.
    """
    return f"{session_key}.full.md" if full else f"{session_key}.md"


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
# working, or stop and answer from what you have). The wording is a phrase here and a single kind
# word in the page's row ("tool-use limit"), because this one stands alone inside brackets in prose
# while that one labels a foldable row a reader can open.
_LOOP_LABELS = {"continuation": "continued", "final_answer": "tool-use limit reached"}


def _loop_line(item: dict, max_payload_chars: Optional[int]) -> str:
    """The injected round as one line of prose for the export.

    The two fallbacks answer differently on purpose, matching what the page does with the same two
    cases. A missing ``reason`` means a producer that did not send the key, and reads as the commoner
    of the pair. An *unrecognized* one is rendered as itself: a label asserting the wrong injection is
    exactly what naming the injection was meant to stop, so a third kind AIMU might add reads as odd
    rather than as a confident lie.

    The prompt is capped like every other model-supplied payload in an export, since a caller may set
    its own (``workflows/critics.py`` does) and nothing bounds how long that can be.
    """
    reason = item.get("reason") or "continuation"
    label = _LOOP_LABELS.get(reason, reason)
    return f"_[{label}: {_capped(item.get('text', ''), max_payload_chars)}]_"


def _ending_line(ending: dict) -> str:
    """How a tool loop ended, as one italic line. Worded as the terminal and the page word it, and like
    them counts rounds from 1 (AIMU's own counter starts at 0); a forced wrap-up names the cap instead."""
    limit = ending.get("max_iterations")
    of_limit = f" of {limit}" if limit is not None else ""
    round_ = ending.get("round")
    position = f"round {round_ + 1 if isinstance(round_, int) else round_}{of_limit}"
    ended_by = ending.get("ended_by")
    if ended_by == "answered":
        return f"_[ended: answered at {position}]_"
    if ended_by == "wrap_up":
        cap = f"limit of {limit} rounds" if limit is not None else "limit"
        return f"_[ended: tool-use {cap} reached; answered on a forced wrap-up call]_"
    if ended_by == "error":
        error = f" ({ending['error']})" if ending.get("error") else ""
        return f"_[ended: error{error} at {position}]_"
    return f"_[ended at {position}]_"


def render_markdown(
    session: Session,
    *,
    max_payload_chars: Optional[int] = DEFAULT_MAX_PAYLOAD_CHARS,
    payloads_path: Optional[Path] = None,
) -> str:
    """The conversation as a Markdown document: a header, then one section per turn.

    ``max_payload_chars`` bounds how much of a tool call's arguments or result is shown before
    it is cut with a note (see :func:`_capped`); pass ``None`` to lift the cap entirely.

    ``payloads_path`` is where a spilled sub-agent tool response can be read back from (see
    ``kokua.payloads`` and ``core/subagents.py``). It is a caller-supplied argument rather than
    something this function resolves from config itself, matching the existing shape of every other
    input here: a pure renderer taking exactly what it needs, so a caller with no such directory (a
    test fixture, or config not yet loaded) can render without one. Passing it only matters when
    ``max_payload_chars`` is ``None``; short of that, a spilled response is only ever previewed and
    noted, never read back, so there is nothing to fetch it for.

    ``payloads_path`` also lets each turn show the system prompts its calls were sent, read from the
    recorded request payloads. A turn shows each agent's prompt from that agent's first readable call
    in the turn, so a prompt that changes between rounds of the same turn is not shown; with no
    ``payloads_path`` no system prompt is shown at all.
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
        mid_turn=metadata.get("messages"),
        undelivered=metadata.get("undelivered"),
        requests=metadata.get("requests"),
        toolsets=metadata.get("toolsets"),
    )
    lines.extend(_render_body(items, metadata, max_payload_chars, payloads_path))
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


def _render_subagent(
    events: list[dict], max_payload_chars: Optional[int], payloads_path: Optional[Path] = None
) -> list[str]:
    """One spawn's whole lifecycle, grouped by its caller into a single card.

    ``events`` is every event recorded for one spawn id, in emission order (see
    ``core/subagents.py``'s ``SubagentReporter``). The first is always the create event, carrying
    the role, the task, and, when configured, the model and reasoning effort; the rest are whatever
    it produced (reasoning, a tool call, generated text) and the terminal status that closed it,
    which may itself carry a final chunk when nothing streamed live. A sub-agent's reasoning and
    generated text are arbitrary model output exactly like a tool result, so they get the same
    fencing and capping: an answer containing its own code fence must not break the rest of the
    document, and a long one must not bury the turn that spawned it.

    ``events`` may also hold ``{"nested": [...]}`` markers, which ``_render_subagent_run`` places for a
    spawn this one's worker made: that spawn's whole card is rendered there as a blockquote, so the
    document shows who spawned whom the way the page does.

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
    # A composed worker's capabilities, which its card carries because its call's tool block is not
    # replayed beside it (see `core/transcripts.py`'s SPAWN_TOOL_NAMES).
    tools = (create.get("arguments") or {}).get("tools")
    if tools:
        details.append("tools " + ", ".join(str(tool) for tool in tools))
    model = create.get("model")
    if model:
        details.append(f"model {model}")
    if "thinking" in create:
        details.append(f"effort {_format_effort(create['thinking'])}")
    if details:
        lines.append("")
        lines.append("_" + ", ".join(details) + "_")
    for event in events[1:]:
        nested = event.get("nested")
        if nested is not None:
            lines.append("")
            child = _render_subagent(nested, max_payload_chars, payloads_path)
            lines.extend(f"> {line}" if line else ">" for line in child)
            continue
        append = event.get("append")
        if append is None:
            continue
        kind = append.get("kind")
        if kind == "request":
            continue  # a round marker is for the page, as at the top level
        lines.append("")
        if kind == "ending":
            lines.append(_ending_line(append))
        elif kind == "tool":
            lines.extend(_render_tool(append, max_payload_chars, payloads_path))
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
            lines.append(_loop_line(append, max_payload_chars))
        elif kind == "message":
            # A message that reached this worker while it ran: the user's own words, or one of the
            # turn's own agents' (`core/messaging.py`). Total loss otherwise, not merely an unlabeled
            # line: a message delivered here never becomes a message in `session.messages` at all
            # (`core/subagents.py`'s own module docstring says why), so this card entry is the *only*
            # place its text exists anywhere in the stored record. No per-sender label the way the
            # top-level `inbox` item gets ("User"/"Agent"/"Mixed" from its own provenance tag): this
            # entry carries no separate sender field to read one off (the same reason `app.js`'s
            # `renderMidTurn` needs none here either -- see `core/subagents.py`'s `INBOX` branch), so
            # an agent's words already carry their own `[message from {sender}]` prefix inline and a
            # user's read bare, exactly as delivered.
            lines.append(f"**Message (mid-turn):** {_capped(append.get('text', ''), max_payload_chars)}")
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


def _render_tool(item: dict, max_payload_chars: Optional[int], payloads_path: Optional[Path] = None) -> list[str]:
    """A tool call's name, arguments, and result, each payload capped and fenced.

    The arguments block gets a ``json`` language hint because the model always emits that shape;
    the result's is left bare, since a tool can return anything from plain text to another
    language's source. A ``response`` of ``None`` means no result was recorded (a turn cancelled
    mid-call), which is worth showing as its own line rather than as an empty, easily-missed block.

    A sub-agent's response can carry ``response_ref``: ``core/subagents.py`` already cut it to
    ``RESPONSE_PREVIEW_CHARS`` before it ever reached storage, spilling the rest to a payload file
    named by ``response_bytes``. That preview is short enough that ``_capped`` never finds it over
    ``max_payload_chars`` (the two happen to share a value), so relying on the usual cap-and-note
    here would silently present a quarter of a result as though it were the whole thing, which is
    exactly what an export must never do. So a ``response_ref`` is always noted, independent of
    length: under ``max_payload_chars=None`` (an export that asked for everything) the blob is read
    back through ``payloads_path`` and shown whole; otherwise, or if the blob is missing, the stored
    preview stands with a note giving its real size and where the rest lives.
    """
    # `toolset.tool` where the call recorded its toolset, as the page shows it; bare where it did not.
    name = f"{item['toolset']}.{item['name']}" if item.get("toolset") else item["name"]
    lines = [f"**Tool call: `{name}`**", ""]
    arguments = item.get("arguments")
    if arguments:
        lines.append(_fenced(_capped(arguments, max_payload_chars), "json"))
    response = item.get("response")
    response_ref = item.get("response_ref")
    lines.append("")
    if response is None:
        lines.append("_(no result recorded)_")
    elif response_ref:
        full_text = None
        if max_payload_chars is None and payloads_path is not None:
            full_text = payloads.read_text(payloads_path, response_ref)
        if full_text is not None:
            lines.append(_fenced(full_text))
        else:
            lines.append(_fenced(response))
            size = item.get("response_bytes")
            size_text = f"{size:,} chars" if isinstance(size, int) else "an unrecorded size"
            note = f"_[preview only: the full result is {size_text}, stored at `{response_ref}`"
            if max_payload_chars is None:
                note += "; the payload file could not be read back"
            note += "]_"
            lines.append(note)
    else:
        lines.append(_fenced(_capped(response, max_payload_chars)))
    return lines


def _render_item(item: dict, max_payload_chars: Optional[int], payloads_path: Optional[Path] = None) -> list[str]:
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
        return [_loop_line(item, max_payload_chars)]
    if item_type == "request":
        return []  # a round marker is for the page; the export's evidence is the turn's own content
    if item_type == "ending":
        return [_ending_line(item)]
    if item_type == "inbox":
        # A label rather than ``loop``'s italic machine note, and uncapped like the turn's own user
        # text: a message sent into a turn that was already running is somebody's words either way. It
        # opens no turn heading of its own because ``replay_items`` gives it no ``message_index``,
        # which is what keeps the turn it joined from reading as two.
        #
        # Who said it has to be read off the item rather than assumed, because one of the turn's own
        # agents can send one too (``core/messaging.py``) and a line signed "User" over a worker's
        # note, or over a delivery that joined the two into one message, is the impersonation the
        # provenance tag exists to stop. "Agent" and "Mixed" rather than a name: the sender's address
        # is not on the stored message, only the fact that it was not the user, or not the user alone.
        speaker = {"agent": "Agent", "mixed": "Mixed"}.get(item.get("from"), "User")
        return [f"**{speaker} (mid-turn):** {item.get('text', '')}"]
    if item_type == "tool":
        return _render_tool(item, max_payload_chars, payloads_path)
    if item_type == "notice":
        # A blockquote, so the reason a turn stopped reads as an aside on whatever it produced
        # before stopping, not as more of that output.
        text = _capped(item.get("text", ""), max_payload_chars)
        return [f"> {line}" for line in text.splitlines()] or ["> "]
    if item_type == "undelivered":
        # A blockquote, like `notice`: this is the turn reporting on itself, not more of what it
        # produced. Unlike `inbox`, this message never reached a stored transcript message at all
        # (``MessageBus.close()``'s own report, surfaced here through ``core/transcripts.py``'s
        # ``undelivered`` metadata), so the sender and the selector are read straight off the item
        # rather than off a provenance tag -- there is no message here for one to ride. Both are
        # named because neither alone tells a reader anything to act on: who sent it, and who it
        # never reached.
        sender = item.get("sender", "")
        to = item.get("to", "")
        text = _capped(item.get("text", ""), max_payload_chars)
        return [f"> **Undelivered:** {sender} -> {to}: {text}"]
    return [f"_(unrendered item type: {item_type})_"]


def _render_subagent_run(
    items: list[dict], start: int, max_payload_chars: Optional[int], payloads_path: Optional[Path] = None
) -> tuple[list[str], int]:
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
                parent = groups.get(event.get("parent"))
                # Placed in its parent's own sequence at the point it was spawned, which is where the
                # page draws it too. A parent missing from this run leaves it a top-level card.
                if parent is not None:
                    parent.append({"nested": group})
                else:
                    order.append(group)
            group.append(event)
        index += 1
    lines: list[str] = []
    for group in order:
        lines.append("")
        if group[0].get("id") is None:
            lines.extend(_render_verdict(group[0], max_payload_chars))
        else:
            lines.extend(_render_subagent(group, max_payload_chars, payloads_path))
    return lines, index


def _render_body(
    items: list[dict], metadata: dict, max_payload_chars: Optional[int], payloads_path: Optional[Path] = None
) -> list[str]:
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
    previous_prompts: dict[str, str] = {}
    requests = metadata.get("requests") or {}
    open_index: object = object()  # sentinel: no real message_index (an int, or None) ever equals it
    index = 0
    while index < len(items):
        item = items[index]
        if item["type"] == "subagent":
            card_lines, index = _render_subagent_run(items, index, max_payload_chars, payloads_path)
            lines.extend(card_lines)
            continue
        lines.append("")
        message_index = item.get("message_index")
        if message_index is not None and message_index != open_index:
            open_index = message_index
            lines.append(f"## Turn {item['turn']}{_when_suffix(item)}")
            meta_line = _turn_meta_line(message_index, metadata)
            if meta_line:
                lines.append("")
                lines.append(meta_line)
            for block in _turn_cost_blocks(message_index, metadata):
                lines.append("")
                lines.extend(block)
            if payloads_path is not None:
                calls = (requests.get(str(message_index)) or {}).get("calls") or []
                for block in _system_prompt_blocks(
                    item["turn"], calls, previous_prompts, max_payload_chars, payloads_path
                ):
                    lines.append("")
                    lines.extend(block)
            lines.append("")
        if item["type"] == "user":
            lines.append(f"**User:** {item['text']}")
        else:
            lines.extend(_render_item(item, max_payload_chars, payloads_path))
        index += 1
    return lines


def system_prompt_of(payload: Any) -> Optional[str]:
    """The system prompt inside one recorded request payload, or ``None`` when it holds none.

    The one place Kokua reads a payload's structure. A payload is whatever AIMU's provider adapter
    built for the wire, so the prompt lives in one of two places: a top-level ``system`` key (a
    string, or a list of text blocks, as Anthropic takes it) or a leading ``messages`` entry with the
    ``system`` role (the OpenAI-compatible shape). A payload with neither, such as an in-process
    provider's raw prompt, has no system prompt this can name, and guessing one would present
    invented text as evidence of what the model was told.
    """
    if not isinstance(payload, dict):
        return None
    system = payload.get("system")
    if system is not None:
        return _joined_text(system)
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                return _joined_text(message.get("content"))
    return None


def _joined_text(content: Any) -> Optional[str]:
    """A string, or a list of blocks whose ``text`` values are joined with blank lines."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block if isinstance(block, str) else block.get("text")
            for block in content
            if isinstance(block, (str, dict))
        ]
        return "\n\n".join(text for text in texts if isinstance(text, str))
    return None


def _read_json(payloads_path: Path, reference: Optional[str]) -> Any:
    """The JSON a payload reference names, or ``None`` when it is absent, gone, or not JSON."""
    if not reference:
        return None
    try:
        text = payloads.read_text(payloads_path, reference)
        return None if text is None else json.loads(text)
    except (OSError, ValueError):
        return None


def _system_prompt_blocks(
    turn: int,
    calls: list[dict],
    previous: dict[str, str],
    max_payload_chars: Optional[int],
    payloads_path: Path,
) -> list[list[str]]:
    """One block per agent whose system prompt is new or changed this turn.

    ``previous`` holds each agent's last shown prompt and is updated in place. It is keyed by agent
    name so a sub-agent and the entry agent never compare against each other: they are told different
    things by design, and a comparison across them would show a prompt on every turn or never.
    """
    seen: set[str] = set()
    blocks = []
    for call in calls:
        agent = call.get("agent") or "agent"
        if agent in seen:
            continue
        payload = _read_json(payloads_path, call.get("payload_ref"))
        prompt = system_prompt_of(payload)
        if prompt is None:
            if payload is not None:
                # Readable but of a shape this cannot read: say so, rather than let the turn look
                # like it ran with no system prompt. A missing or unreadable payload stays silent.
                seen.add(agent)
                blocks.append([f"_(system prompt not found in the recorded request; see kokua export --turn {turn})_"])
            continue
        seen.add(agent)
        if previous.get(agent) == prompt:
            continue
        previous[agent] = prompt
        blocks.append([f"**System prompt ({agent}):**", "", _fenced(_capped(prompt, max_payload_chars))])
    return blocks


def _call_heading_lines(call: dict) -> list[str]:
    """A call's heading and its one-line facts: run, provider, where it went, model, how it stopped."""
    endpoint = call.get("endpoint") or "no wire request (nearest equivalent; not replayable)"
    facts = [
        f"run `{call.get('run_id')}`",
        f"provider {call.get('provider')}",
        f"endpoint {endpoint}",
        f"model {call.get('model')}",
        f"stop reason {call.get('stop_reason')}",
    ]
    if call.get("error"):
        facts.append(f"error {call['error']}")
    round_ = call.get("round")
    shown_round = round_ + 1 if isinstance(round_, int) else round_
    return [f"### {call.get('agent') or 'agent'} · round {shown_round}", "", "- " + ", ".join(facts)]


def _response_lines(response: Any) -> list[str]:
    parts = []
    if response.get("text"):
        parts += ["Text:", "", _fenced(str(response["text"]))]
    if response.get("thinking"):
        parts += ["Reasoning:", "", _fenced(str(response["thinking"]))]
    if response.get("tool_calls"):
        parts += [
            "Tool calls:",
            "",
            _fenced(json.dumps(response["tool_calls"], indent=2, ensure_ascii=False, default=str), "json"),
        ]
    return parts or ["_empty response_"]


def render_turn_markdown(session: Session, turn: int, *, payloads_path: Path) -> str:
    """One turn in full: the user's message, then every model call it made, request and response.

    The conversation export shows what happened; this shows what the model was sent. Each call's
    payload is the request exactly as recorded, pretty-printed and never capped, since cutting the
    evidence defeats the purpose of asking for it. A payload or response that is missing or unreadable
    (a user cleared the folder, or the call predates capture) reads ``_not captured_`` rather than
    failing the export.

    Raises ``ValueError`` naming the valid range when ``turn`` is not one the conversation has.
    """
    index_of_turn = {ordinal: index for index, ordinal in turn_numbers(session.messages, session.metadata).items()}
    if not index_of_turn:
        raise ValueError("this conversation has no turns")
    if turn not in index_of_turn:
        raise ValueError(f"turn {turn} is out of range; this conversation has turns 1 to {len(index_of_turn)}")
    user_index = index_of_turn[turn]
    title = session.metadata.get("title") or derive_title(session.messages) or "Untitled conversation"
    lines = [f"# {title}: turn {turn}", "", f"**User:** {message_text(session.messages[user_index].get('content'))}"]

    record = (session.metadata.get("requests") or {}).get(str(user_index)) or {}
    calls = record.get("calls") or []
    entry_run_id = record.get("entry_run_id")
    run_ids = list(dict.fromkeys(call.get("run_id") for call in calls))
    if entry_run_id is not None and entry_run_id in run_ids:
        run_ids.remove(entry_run_id)
        run_ids.insert(0, entry_run_id)
    if not calls:
        lines += ["", "_No model calls were recorded for this turn._"]
    for run_id in run_ids:
        for call in (call for call in calls if call.get("run_id") == run_id):
            lines += [""] + _call_heading_lines(call)
            request = _read_json(payloads_path, call.get("payload_ref"))
            lines += ["", "**Request:**", ""]
            lines.append(
                "_not captured_"
                if request is None
                else _fenced(json.dumps(request, indent=2, ensure_ascii=False), "json")
            )
            response = _read_json(payloads_path, call.get("response_ref"))
            lines += ["", "**Response:**", ""]
            lines += ["_not captured_"] if not isinstance(response, dict) else _response_lines(response)
    for ending in record.get("endings") or []:
        lines += ["", _ending_line(ending)]
    return "\n".join(lines) + "\n"
