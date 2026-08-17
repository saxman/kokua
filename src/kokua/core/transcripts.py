"""Reading a stored conversation as text: what was said, flattened, truncated, and searched.

This is the presentation-free half of cross-conversation reading. It turns AIMU's stored message dicts
into plain lines and finds text in them; it does not decide how many lines a caller may have, what a
missing conversation should be told, or how a match should be introduced. Those are the ``conversations``
toolset's, in ``toolsets/conversations.py``.

Every function here takes messages or sessions and returns data, so a front end that wanted to show a
transcript could use the same ones the agent tools do.
"""

from __future__ import annotations

from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER, PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.sessions import Session

from kokua.core.messages import message_text

# Cut one message on its own, before any whole-transcript budget, so a single pasted document cannot
# consume a read and hide every message around it.
MAX_MESSAGE_CHARS = 2_000

# User-role turns the agent loop injects between tool-calling iterations: not the user's words, so they
# are left out of a transcript. The display-side twin is ``channels.web._LOOP_PROVENANCE``.
_INJECTED_USER_PROVENANCE = frozenset({PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER})
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
        if role == "user" and provenance in _INJECTED_USER_PROVENANCE:
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
