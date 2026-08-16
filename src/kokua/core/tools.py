"""Agent tools over core runtime state. Currently: reading across the user's other conversations.

This is `core/`'s entry in the ``<subsystem>/tools.py`` convention (see ``docs/explanation/
architecture.md`` for the full inventory of the entry agent's toolset and where each group comes from).
A tool group belongs here when it needs live objects the ``core/`` composition root owns and no other
subsystem does; anything scoped to config, scheduling, or MCP goes in that subsystem's ``tools.py``
instead.

``make_conversation_tools`` builds ``list_conversations``, ``read_conversation``, and
``search_conversations``, which let the entry agent answer "what did we decide about X last week?" and
carry context out of a past thread into a `spawn_subagent` task. They are entry-agent-only, like the
other cross-cutting tools: a worker shares no history and has no conversation identity, so "the user's
other conversations" only means something to the agent the user is talking to.

Registered below as ``TOOLSET`` (see ``kokua.toolsets.core.CORE_TOOLSETS``), resolved through the same
registry as every other capability: ``ToolsetContext`` carries the live ``ConversationBook`` and the
assistant's ``turn_running`` off ``LiveState``, which is what lets ``make_conversation_tools`` build
these without this module importing ``core.assistant`` directly.

Every read goes through the session store, never ``ConversationBook.agent_for``; see
``make_conversation_tools`` for why, and for the two markers that keep a store snapshot honest.
"""

from __future__ import annotations

from typing import Callable, Optional

from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER, PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.sessions import Session
from aimu.tools import tool

from kokua.core.conversations import ConversationBook
from kokua.core.messages import message_text
from kokua.toolsets.registry import Toolset

DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT = 30, 200
DEFAULT_READ_CHARS, MAX_READ_CHARS = 8_000, 40_000
MIN_READ_CHARS = 200
# Cut one message on its own, before the whole-transcript budget, so a single pasted document cannot
# consume a read and hide every message around it.
MAX_MESSAGE_CHARS = 2_000
DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS = 20, 50
DEFAULT_CONTEXT_CHARS, MAX_CONTEXT_CHARS = 100, 400
SNIPPETS_PER_CONVERSATION = 2
# A model that saw a 32-hex id in a list is apt to shorten it; answering "no such conversation" would
# just send it round the loop. Long enough that a prefix hit is not a coincidence.
ID_PREFIX_MIN = 6

ACTIVE_CONVERSATION_NOTE = (
    "This is the conversation you are in. Its current turn is not saved yet, so use your own context "
    "for anything said in this turn."
)
RUNNING_TURN_NOTE = "[... a turn is still running in this conversation; its messages are not saved yet ...]"
NO_CONVERSATIONS = "No saved conversations."
BLANK_QUERY = "Give some text to search for."

# User-role turns the agent loop injects between tool-calling iterations: not the user's words, so they
# are left out of a transcript. The display-side twin is ``channels.web._LOOP_PROVENANCE``.
_INJECTED_USER_PROVENANCE = frozenset({PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER})
# The system message is the agent's own guidance, and a ``tool`` message is trace rather than conversation.
_SKIPPED_ROLES = frozenset({"system", "tool"})


def _clamp(value, low: int, high: int, default: int) -> int:
    """A model-supplied bound, coerced and clamped to ``[low, high]``.

    Anything unusable -- non-numeric, or below ``low`` (which includes zero and negatives) -- falls back
    to ``default`` instead of failing the call: a bad bound is not worth costing the model a turn.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < low:
        return default
    return min(number, high)


def _short_time(timestamp) -> str:
    """An ISO timestamp as ``YYYY-MM-DD HH:MM``, or ``""`` when there isn't one.

    Never converted or reformatted beyond truncation, so the times here match what the web UI shows
    (both read the same local, naive ``datetime.now().isoformat()`` strings).
    """
    if not isinstance(timestamp, str) or len(timestamp) < 16:
        return ""
    return timestamp[:16].replace("T", " ")


def _title_of(session: Session) -> str:
    return session.metadata.get("title") or "New conversation"


def _image_placeholders(content) -> str:
    """``" [image]"`` per image block, so an image-only turn does not read as an empty message.

    The stored url is a content-addressed ``/images/<hash>`` reference, which means nothing to a model,
    so it is deliberately not included.
    """
    if not isinstance(content, list):
        return ""
    images = sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image_url")
    return " [image]" * images


def _readable_messages(messages: list[dict]) -> list[tuple[str, object, str]]:
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
    for label, timestamp, text in _readable_messages(messages):
        when = _short_time(timestamp)
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
    for label, _timestamp, text in _readable_messages(messages):
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
    for label, _timestamp, text in _readable_messages(messages):
        haystack = text.lower()
        if not all(term in haystack for term in lowered):
            continue
        position, length = min((haystack.find(term), len(term)) for term in lowered)
        snippets.append(f"{label}: {_snippet(text, position, position + length, context_chars)}")
        if len(snippets) >= limit:
            break
    return snippets


def make_conversation_tools(book: ConversationBook, turn_running: Callable[[str], bool]) -> list[Callable]:
    """Build the read-only cross-conversation tools bound to the live ``ConversationBook``.

    Every read goes through the book's *store*, never ``agent_for``. Reading is meant to be cheap and
    side-effect-free, and building an agent is neither: it allocates a model client, re-expands every
    stored image to base64, mutates the LRU registry (so reading twenty conversations would evict the
    live agents of conversations with running turns), and can raise ``ModelClientError`` for reasons that
    have nothing to do with reading. The store is also the only coherent source: a running turn appends
    to ``agent.model_client.messages`` in place, so a reader in another task can observe a half-written
    turn, while the store holds a snapshot written once, at the end of a turn. For a quiescent
    conversation the agent's transcript *is* the stored one re-expanded, so nothing is lost.

    Two markers keep that snapshot honest, since it can lag what the user sees: ``turn_running``
    (the assistant's accessor) flags a conversation whose in-flight turn is not saved yet, and the active
    conversation is flagged as the one whose current turn the model should read out of its own context.

    Not touching the registry also means this factory is safe to build before
    ``ConversationBook.bind_registry`` -- which it is, since the book is constructed first.
    """

    def _resolve(conversation_id: str) -> Optional[Session]:
        """The stored session for an id, or ``None``.

        Existence is checked with ``book.exists`` (the store's key list) and never ``get`` alone, which
        returns an empty ``Session`` for a missing key and would present a deleted conversation as a
        blank one. A unique leading fragment of at least ``ID_PREFIX_MIN`` characters also resolves; an
        ambiguous one does not, so the model is told rather than handed the wrong conversation.
        """
        wanted = (conversation_id or "").strip().strip("'\"`")
        if not wanted:
            return None
        if book.exists(wanted):
            return book.get(wanted)
        if len(wanted) < ID_PREFIX_MIN:
            return None
        matches = [session for session in book.sessions() if session.key.startswith(wanted)]
        return matches[0] if len(matches) == 1 else None

    def _unknown(conversation_id: str) -> str:
        return (
            f"No conversation matches {conversation_id!r}. "
            "Call `list_conversations` or `search_conversations` for current ids."
        )

    def _marks(session: Session) -> str:
        marks = " (current)" if session.key == book.active_id else ""
        return marks + (" (turn in progress)" if turn_running(session.key) else "")

    @tool
    async def list_conversations(limit: int = DEFAULT_LIST_LIMIT) -> str:
        """List the user's saved chat conversations, most recently active first.

        Each line gives the conversation id, when it was last active, how many messages it holds, and its
        title (derived from its first user message). The conversation you are talking in right now is
        marked "(current)" -- its transcript is already in your context, so do not read it back. One
        marked "(turn in progress)" has a reply still being generated, so its saved transcript stops
        short of that turn. Pass an id to `read_conversation`, or use `search_conversations` to find one
        by what was said in it. These are chat threads, not scheduled tasks; see `list_scheduled_tasks`
        for those.

        Args:
            limit: How many conversations to list, newest first. Defaults to 30, capped at 200.
        """
        sessions = book.sessions()
        if not sessions:
            return NO_CONVERSATIONS
        shown = sessions[: _clamp(limit, 1, MAX_LIST_LIMIT, DEFAULT_LIST_LIMIT)]
        lines = [
            f"- {session.key} | {_short_time(session.metadata.get('updated_at'))} "
            f"| {len(_readable_messages(session.messages))} messages | {_title_of(session)}{_marks(session)}"
            for session in shown
        ]
        hidden = len(sessions) - len(shown)
        if hidden:
            lines.append(f"({hidden} older conversations not shown; raise limit to see them.)")
        return "\n".join(lines)

    @tool
    async def read_conversation(conversation_id: str, max_chars: int = DEFAULT_READ_CHARS) -> str:
        """Read one saved conversation's transcript as plain text, oldest message first.

        Give an id from `list_conversations` or `search_conversations` (a unique leading fragment of at
        least six characters also works). The transcript holds only what was said -- each user and
        assistant message, prefixed with its role and time -- with reasoning, tool calls, and tool output
        left out, and each image shown as "[image]". If the whole transcript does not fit in `max_chars`,
        the OLDEST messages are dropped and a marker at the top says how many, so what you get always
        ends with the most recent message. This only reads: it does not switch the user's view, resume
        the conversation, or change anything.

        Args:
            conversation_id: The conversation to read (full id, or a unique leading fragment).
            max_chars: Character budget for the transcript. Defaults to 8000, capped at 40000.
        """
        session = _resolve(conversation_id)
        if session is None:
            return _unknown(conversation_id)
        lines = flatten_transcript(session.messages)
        header = [
            f"Conversation {session.key} -- {_title_of(session)} "
            f"({len(lines)} messages, last active {_short_time(session.metadata.get('updated_at'))})"
        ]
        if session.key == book.active_id:
            header.append(ACTIVE_CONVERSATION_NOTE)
        if not lines:
            return "\n".join(header + ["(no messages)"])
        kept, dropped = truncate_lines(lines, _clamp(max_chars, MIN_READ_CHARS, MAX_READ_CHARS, DEFAULT_READ_CHARS))
        if dropped:
            header.append(
                f"[... {dropped} older messages omitted to fit max_chars; "
                f"raise max_chars (up to {MAX_READ_CHARS}) to see more ...]"
            )
        # The running-turn note goes last because that turn is newer than everything shown.
        footer = [RUNNING_TURN_NOTE] if turn_running(session.key) else []
        return "\n".join(header + [""] + kept + footer)

    @tool
    async def search_conversations(
        query: str,
        max_results: int = DEFAULT_SEARCH_RESULTS,
        context_chars: int = DEFAULT_CONTEXT_CHARS,
    ) -> str:
        """Search what was said across every saved conversation and return the ones that match.

        The match is case-insensitive text. First every conversation containing `query` as a phrase; if
        nothing contains the phrase and `query` is several words, every conversation with a single
        message containing all of those words. Each result gives the conversation id, its title, when it
        was last active, and up to two snippets with surrounding text, so you can pick which one to open
        with `read_conversation`. Reasoning, tool calls, and tool output are not searched. The current
        conversation is searched only as far as it is saved: nothing said in this turn is findable yet.

        Args:
            query: Text to look for. A short distinctive phrase or a few keywords works best.
            max_results: How many matching conversations to return, newest first. Defaults to 20, capped at 50.
            context_chars: Characters of surrounding text on each side of a match. Defaults to 100, capped at 400.
        """
        needle = (query or "").strip()
        if not needle:
            return BLANK_QUERY
        width = _clamp(context_chars, 1, MAX_CONTEXT_CHARS, DEFAULT_CONTEXT_CHARS)
        wanted = _clamp(max_results, 1, MAX_SEARCH_RESULTS, DEFAULT_SEARCH_RESULTS)
        sessions = book.sessions()

        def find(matcher) -> list[tuple[Session, list[str]]]:
            hits = []
            for session in sessions:
                snippets = matcher(session.messages)
                if snippets:
                    hits.append((session, snippets))
            return hits

        hits = find(
            lambda messages: phrase_matches(messages, needle, context_chars=width, limit=SNIPPETS_PER_CONVERSATION)
        )
        # A model tends to pass a natural phrase no message contains verbatim ("dentist appointment
        # tuesday"), and "nothing matches" is then actively misleading. The fallback says so in the
        # output, so the model is never misled about which semantics produced a hit.
        terms = needle.split()
        preamble = []
        if not hits and len(terms) > 1:
            hits = find(
                lambda messages: term_matches(messages, terms, context_chars=width, limit=SNIPPETS_PER_CONVERSATION)
            )
            if hits:
                preamble = [f"No conversation contains {needle!r} verbatim; these mention all of its words."]
        if not hits:
            return f"Nothing in the saved conversations matches {needle!r}."

        blocks = []
        for session, snippets in hits[:wanted]:
            blocks.append(
                f"- {session.key} | {_short_time(session.metadata.get('updated_at'))} "
                f"| {_title_of(session)}{_marks(session)}"
            )
            blocks.extend(f"    {snippet}" for snippet in snippets)
        hidden = len(hits) - len(hits[:wanted])
        if hidden:
            blocks.append(f"({hidden} more matching conversations not shown; narrow the query or raise max_results.)")
        return "\n".join(preamble + blocks)

    return [list_conversations, read_conversation, search_conversations]


CONVERSATIONS_GUIDANCE = (
    " You can see across the user's other chat conversations with `list_conversations`, "
    "`read_conversation`, and `search_conversations`, which read their saved transcripts. They are "
    "read-only, and this turn is not saved yet, so use your own context for the conversation you are in."
)

TOOLSET = Toolset(
    name="conversations",
    description="Read-only visibility across the user's other conversations.",
    build=lambda ctx: make_conversation_tools(ctx.state.conversation_book, ctx.state.turn_running),
    guidance=CONVERSATIONS_GUIDANCE,
    cross_cutting=True,
)
