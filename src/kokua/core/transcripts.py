"""Reading a stored conversation as text: what was said, flattened, truncated, and searched.

This is the presentation-free half of cross-conversation reading. It turns AIMU's stored message dicts
into plain lines and finds text in them; it does not decide how many lines a caller may have, what a
missing conversation should be told, or how a match should be introduced. Those are the ``conversations``
toolset's, in ``toolsets/conversations.py``.

Every function here takes messages or sessions and returns data, so a front end that wanted to show a
transcript could use the same ones the agent tools do.

Two readings live here. ``readable_messages`` and ``flatten_transcript`` give the plain what-was-said
view the agent's conversation tools read. ``replay_items`` gives the full one: reasoning, tool calls
paired with their results, sub-agent cards, phase segments, and the notice closing a turn that failed.
The web channel renders it as display frames and the Markdown export renders it as prose, which is why
it sits here rather than in either of them.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from aimu.models import PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.sessions import Session

from kokua.core.messages import INJECTED_USER_PROVENANCE, message_text

# Cut one message on its own, before any whole-transcript budget, so a single pasted document cannot
# consume a read and hide every message around it.
MAX_MESSAGE_CHARS = 2_000

# The system message is the agent's own guidance, and a ``tool`` message is trace rather than conversation.
_SKIPPED_ROLES = frozenset({"system", "tool"})


def short_time(timestamp) -> str:
    """An ISO timestamp as ``YYYY-MM-DD HH:MM``, or ``""`` when there isn't one.

    Never converted or reformatted beyond truncation, so the times here match what the web UI shows
    (both read the same local, naive ``datetime.now().isoformat()`` strings).
    """
    if not isinstance(timestamp, str) or len(timestamp) < 16:
        return ""
    return timestamp[:16].replace("T", " ")


def _image_placeholders(content) -> str:
    """``" [image]"`` per image block, so an image-only turn does not read as an empty message.

    The stored url is a content-addressed ``/images/<hash>`` reference, which means nothing to a model,
    so it is deliberately not included.
    """
    if not isinstance(content, list):
        return ""
    images = sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image_url")
    return " [image]" * images


def readable_messages(messages: list[dict]) -> list[tuple[str, object, str]]:
    """The ``(label, timestamp, text)`` of each message that carries something the user or the assistant
    actually said, in order.

    Skips the system message, tool results, the loop's injected user turns, and any message left with no
    text -- which drops an assistant message whose only content was ``tool_calls``. The cost is that a
    turn whose visible work was all delegation shows only its final answer; that is acceptable because
    the loop ends every turn with a text answer, and the web UI is where the full trace is inspectable.
    ``thinking`` is never read.
    """
    readable: list[tuple[str, object, str]] = []
    for message in messages:
        role = message.get("role")
        if role in _SKIPPED_ROLES:
            continue
        provenance = message.get(PROVENANCE_KEY)
        if role == "user" and provenance in INJECTED_USER_PROVENANCE:
            continue
        content = message.get("content")
        text = (message_text(content) + _image_placeholders(content)).strip()
        if not text:
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            text = f"{text[:MAX_MESSAGE_CHARS]}... [message truncated, {len(text)} chars total]"
        label = "assistant (proactive)" if role == "assistant" and provenance == PROVENANCE_PROACTIVE else str(role)
        readable.append((label, message.get("timestamp"), text))
    return readable


def flatten_transcript(messages: list[dict]) -> list[str]:
    """One ``[time] role: text`` entry per readable message, oldest first.

    The time bracket is omitted when the message has no ``timestamp`` (transcripts persisted before
    AIMU's inert timestamping shipped). Internal newlines are kept, so an entry may wrap over several
    display lines; search collapses whitespace in its snippets instead.
    """
    lines = []
    for label, timestamp, text in readable_messages(messages):
        when = short_time(timestamp)
        lines.append(f"[{when}] {label}: {text}" if when else f"{label}: {text}")
    return lines


def truncate_lines(lines: list[str], budget: int) -> tuple[list[str], int]:
    """The newest entries that fit in ``budget`` characters, plus how many older ones were dropped.

    Trims from the oldest end so the newest content is last and the result still reads in order. Always
    keeps at least one entry, so a single over-budget message returns something rather than nothing.
    """
    kept: list[str] = []
    spent = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if kept and spent + cost > budget:
            break
        kept.append(line)
        spent += cost
    kept.reverse()
    return kept, len(lines) - len(kept)


def _snippet(text: str, start: int, end: int, context_chars: int) -> str:
    """``text[start:end]`` with surrounding context, whitespace collapsed, ellipsed only where cut."""
    left = max(0, start - context_chars)
    right = min(len(text), end + context_chars)
    body = " ".join(text[left:right].split())
    return f"{'...' if left > 0 else ''}{body}{'...' if right < len(text) else ''}"


def phrase_matches(messages: list[dict], needle: str, *, context_chars: int, limit: int) -> list[str]:
    """``role: ...snippet...`` for each message containing ``needle``, case-insensitively.

    At most one snippet per message, so one repetitive message cannot crowd out the others. Scans exactly
    the text ``flatten_transcript`` shows, so anything found here is findable in a read.
    """
    lowered = needle.lower()
    snippets = []
    for label, _timestamp, text in readable_messages(messages):
        position = text.lower().find(lowered)
        if position < 0:
            continue
        snippets.append(f"{label}: {_snippet(text, position, position + len(needle), context_chars)}")
        if len(snippets) >= limit:
            break
    return snippets


def term_matches(messages: list[dict], terms: list[str], *, context_chars: int, limit: int) -> list[str]:
    """``role: ...snippet...`` for each message containing every one of ``terms``, case-insensitively.

    All the terms must appear in the *same* message: a conversation that mentions one term early and
    another much later is almost never the one being looked for. The snippet is anchored on whichever
    term appears earliest.
    """
    lowered = [term.lower() for term in terms]
    snippets = []
    for label, _timestamp, text in readable_messages(messages):
        haystack = text.lower()
        if not all(term in haystack for term in lowered):
            continue
        position, length = min((haystack.find(term), len(term)) for term in lowered)
        snippets.append(f"{label}: {_snippet(text, position, position + length, context_chars)}")
        if len(snippets) >= limit:
            break
    return snippets


def search(
    sessions: list[Session],
    query: str,
    *,
    context_chars: int,
    snippets_per_conversation: int,
) -> tuple[list[tuple[Session, list[str]]], bool]:
    """Matching sessions with their snippets, and whether the term fallback produced them.

    Phrase first. If nothing contains the phrase and the query is several words, retry requiring every
    word in one message: a caller tends to pass a natural phrase no message contains verbatim ("dentist
    appointment tuesday"), where "nothing matches" is actively misleading.

    The flag is returned rather than folded into a message because which semantics produced a hit is
    something the caller must be able to say. A caller told "these mention all of its words" reads the
    results correctly; one told nothing reads a loose match as an exact one.
    """
    needle = query.strip()

    def hits(matcher) -> list[tuple[Session, list[str]]]:
        found = []
        for session in sessions:
            snippets = matcher(session.messages)
            if snippets:
                found.append((session, snippets))
        return found

    found = hits(
        lambda messages: phrase_matches(messages, needle, context_chars=context_chars, limit=snippets_per_conversation)
    )
    if found:
        return found, False
    terms = needle.split()
    if len(terms) < 2:
        return [], False
    fallback = hits(
        lambda messages: term_matches(messages, terms, context_chars=context_chars, limit=snippets_per_conversation)
    )
    # The flag says these results came from the fallback, so an empty fallback reports False: there is
    # nothing for the caller to qualify.
    return fallback, bool(fallback)


# --- replay --------------------------------------------------------------------------------------

# User-role turns the agent loop injects between tool-calling iterations. They are byte-for-byte
# ordinary user messages except for this provenance tag, so display keys off the tag alone. Same set
# as messages.INJECTED_USER_PROVENANCE (both name "a loop injection, not real user input"), kept under
# its own name here because the two call sites ask different questions of it: that one filters a
# message out of what counts as user input, this one decides how to render one that was not filtered.
_LOOP_PROVENANCE = INJECTED_USER_PROVENANCE

# AIMU's make_async_subagent_tool (aimu/aio/tools/builtin.py) defaults its built tool's name to this
# literal; kokua never overrides it. A spawn's own `subagent` card already shows its role, task, and
# result, so the parent's `tool` frame for this one tool name is pure duplication and is suppressed
# wherever a tool call becomes a display frame: `WebChannel.send_frame` and `replay_items`'s replay of a
# stored message's tool_calls below agree on suppressing it because both read this one constant.
# `core/build.py` imports it too, to find and replace the tool on a runtime rebuild; `channels.web`
# imports it locally inside `send_frame`, since a top-level import back there would be circular (see
# that method's comment).
SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"

# A stored image reference: our own /images/<name> route, the compacted form persisted in place of inline
# base64 (see images.py / messages.compact_message_images). Bounded to a bare filename (no slashes) so the match
# can't run past the reference into surrounding prose.
_IMAGE_REF_RE = re.compile(r"/images/[\w.\-]+")


def image_refs_of(content: Any) -> list[str]:
    """Return the image references in a message's content: image_url block urls plus any /images/ refs in text."""
    refs: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url")
                if url:
                    refs.append(url)
    refs.extend(_IMAGE_REF_RE.findall(message_text(content)))
    return list(dict.fromkeys(refs))  # de-dupe, preserving order


def _tool_results_by_call_id(messages: list[dict]) -> dict[str, str]:
    """Map each tool result in ``messages`` to the id of the call it answers.

    A live ``tool`` frame carries the call and its result together (AIMU emits ``TOOL_CALLING`` only once
    the call has returned), but a stored transcript splits them across an assistant message's
    ``tool_calls`` and a later ``role: "tool"`` message, joined by id. Concurrent dispatch appends those
    results in completion order, so the join has to be by id and not by position.
    """
    return {
        message["tool_call_id"]: str(message.get("content", ""))
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }


def replay_items(
    messages: list[dict],
    *,
    subagent: Optional[dict] = None,
    trace: Optional[dict] = None,
    failure: Optional[dict] = None,
) -> list[dict]:
    """Flatten stored conversation messages into ordered display items the page replays on reload.

    Mirrors the live stream order per assistant message: reasoning, then the answer, then the tool calls.
    That is the order the message was written in -- a model emits its prose and then the calls it decided
    to make -- so a reload leaves a turn where the user watched it arrive instead of sinking its prose
    below the cards. The system message is omitted (live chat shows none).

    A ``role: "tool"`` message is not replayed as an item of its own, but its content is rejoined to the
    call it answers (see :func:`_tool_results_by_call_id`) and rides that call's item as ``response``, so
    a replayed tool card carries the output a live one showed. ``response`` is ``None`` where no result
    message exists -- a transcript stored before results were replayed, or a turn cut short mid-dispatch
    -- and the page then renders the card exactly as it always did.

    Two per-turn maps key a user-message index (as a string) to that turn's recorded reviewer activity,
    interleaved right after the user bubble so it replays in place:
      - ``subagent``: summary verdict cards (non-verbose turns). A verbose turn's plan reviewers show
        up in ``trace`` instead, but its executor can still spawn its own sub-agents, and those cards
        (identified by ``task`` on the create event or ``append`` on later ones) replay regardless.
      - ``trace``: the full raw verbose trace as ``phase`` + ``reasoning`` items. A traced turn shows
        the raw output instead of reviewer cards, and its trace already ends with the final answer, so
        the committed assistant message for that turn is skipped to avoid showing the answer twice.

    ``failure`` is keyed the same way, but replays at the *end* of its turn rather than after the user
    bubble: it says why the turn stopped, which only reads correctly after whatever the turn managed to
    produce. It matters most for a scheduled run, whose error never reached this conversation live -- the
    status line for a firing goes to whichever conversation the user was viewing at the time.

    ``message_index`` (the user message's own position in ``messages``, what ``record_turn_provenance``
    keys a turn's model/effort/usage under) is stamped on every item this function emits for a user
    message, not only its text: a message sent with an image and no text yields no ``"user"`` item at
    all, and the Markdown export opens a turn's heading at whichever item carries this key first, so an
    image-only turn still needs one somewhere to anchor to.
    """
    subagent = subagent or {}
    trace = trace or {}
    failure = failure or {}
    items: list[dict] = []
    results = _tool_results_by_call_id(messages)
    pending_failure: Optional[tuple[str, object]] = None  # (reason, the turn's timestamp)

    def add(item: dict, timestamp) -> None:
        # Attach the source message's append-time timestamp (AIMU's inert ``timestamp`` key) so the page
        # can caption the bubble. Omitted when absent (messages persisted before timestamping shipped),
        # so those simply render no caption. Metadata-derived items (phase/reasoning/subagent) pass the
        # turn's user-message timestamp, since they have no timestamp of their own.
        if timestamp:
            item["ts"] = timestamp
        items.append(item)

    def flush_failure() -> None:
        """Close the turn in progress with its recorded reason, if it had one.

        Held until the turn ends rather than emitted where it is read, because the reason belongs after
        the output it cut short. A turn ends at the next user message or at the end of the transcript,
        so this is called from both places; a conversation the user carried on in after a failed turn
        therefore keeps the notice inside that turn instead of trailing it off the bottom.
        """
        nonlocal pending_failure
        if pending_failure is not None:
            reason, turn_ts = pending_failure
            pending_failure = None
            add({"type": "notice", "text": reason}, turn_ts)

    for index, message in enumerate(messages):
        role = message.get("role")
        provenance = message.get(PROVENANCE_KEY)
        ts = message.get("timestamp")
        if role == "user":
            if provenance in _LOOP_PROVENANCE:
                # A framework-injected continuation/final-answer turn, not user input. Show a loop
                # marker carrying the injected prompt text (for inspection) and which injection it was,
                # not a user bubble. `reason` is the provenance itself, which is the same value the live
                # channel reads off AIMU's CONTINUING chunk, so a replayed turn and a watched one render
                # identically. It continues the turn already in progress rather than starting a new one,
                # so it must not close that turn's failure notice either.
                add({"type": "loop", "reason": provenance, "text": message_text(message.get("content"))}, ts)
                continue
            flush_failure()  # whatever turn was in progress ends where this one begins
            if str(index) in failure:
                pending_failure = (failure[str(index)], ts)
            text = message_text(message.get("content"))
            if text:
                # Stamped with this message's position in the transcript, the key record_turn_provenance
                # writes a turn's model, effort, usage, and failure under. A caller (the Markdown export)
                # joins on this instead of deriving the index itself, which is how the off-by-one
                # resolve_user_index's docstring warns about would come back in a second place.
                add({"type": "user", "text": text, "message_index": index}, ts)
            for url in image_refs_of(message.get("content")):  # uploaded images, replayed under the bubble
                # Carries the same message_index as the text item above (or is the only item to carry
                # it, when the user sent an image with no text): the renderer opens a turn at the first
                # item bearing this index, and an image-only user message must still open one.
                add({"type": "image", "url": url, "from": "user", "message_index": index}, ts)
            events = subagent.get(str(index), [])
            if str(index) in trace:  # verbose turn: replay the raw trace, not the verdict cards
                for segment in trace[str(index)]:
                    add({"type": "phase", "label": segment.get("label", ""), "detail": segment.get("detail", "")}, ts)
                    if segment.get("text"):
                        add({"type": "reasoning", "text": segment["text"]}, ts)
                # A reviewer's verdict is already in the trace, but a sub-agent the turn spawned is
                # not, so those cards are replayed on their own. A spawn is identified by the `task` on
                # its create event and the rest of its lineage by that event's id: shape alone is not
                # enough, since a spawn whose text streamed closes with a status-only event that looks
                # exactly like a reviewer's, and dropping it strands the card at "working...".
                spawned = {event["id"] for event in events if "task" in event and "id" in event}
                events = [event for event in events if event.get("id") in spawned]
            for event in events:
                add({"type": "subagent", **event}, ts)
        elif role == "assistant":
            if str(index - 1) in trace:
                # The preceding user turn was verbose; its trace already contains this final answer
                # (in its last Executor phase), so don't emit it again as a separate message.
                continue
            if message.get("thinking"):
                add({"type": "thinking", "text": message["thinking"]}, ts)
            text = message_text(message.get("content"))
            if text:
                add({"type": "message", "text": text, "proactive": provenance == PROVENANCE_PROACTIVE}, ts)
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                name = fn.get("name")
                if name == SPAWN_SUBAGENT_TOOL_NAME:
                    continue  # shown as its own subagent card instead; see SPAWN_SUBAGENT_TOOL_NAME
                add(
                    {
                        "type": "tool",
                        "name": name,
                        "arguments": fn.get("arguments"),
                        "response": results.get(call.get("id")),
                    },
                    ts,
                )
        elif role == "tool":
            # Tool results are otherwise not replayed, but a generate_image result carries an /images/
            # reference the user asked to see, so surface it as an image of its own.
            for url in image_refs_of(message.get("content")):
                add({"type": "image", "url": url, "from": "assistant"}, ts)
    flush_failure()  # the last turn ends at the end of the transcript
    return items
