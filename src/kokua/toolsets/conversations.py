"""The ``conversations`` toolset: read-only sight of the user's other conversations.

``list_conversations``, ``read_conversation`` and ``search_conversations`` let the entry agent answer
"what did we decide about X last week?" and carry context out of a past thread into a `spawn_subagent`
task. They are entry-agent-only, like the other cross-cutting tools: a worker shares no history and has
no conversation identity, so "the user's other conversations" only means something to the agent the user
is talking to.

Everything here is presentation. The transcript reading, flattening, and searching are in
``core/transcripts.py``, and resolving an id or a unique prefix is ``ConversationBook.resolve``. What is
left in this module is the tool schemas, the bounds a model may pass and how they are clamped, and the
sentences it reads back -- including the two markers that keep a stored snapshot honest.

``ToolsetContext`` carries the live ``ConversationBook`` and the assistant's ``turn_running`` off
``LiveState``, which is what lets this module build the tools without importing ``core.assistant``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from aimu.sessions import Session
from aimu.tools import tool

from kokua.core.transcripts import flatten_transcript, readable_messages, search, short_time, truncate_lines
from kokua.registry.registry import Toolset

if TYPE_CHECKING:
    # Annotation only. A real import would run kokua/core/__init__.py, and this module is reached from
    # toolsets/core.py, which core/build.py imports -- the cycle core/build.py and core/assistant.py
    # already dodge with deferred imports. LiveState leaves the same field untyped for the same reason.
    from kokua.core.conversations import ConversationBook

DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT = 30, 200
DEFAULT_READ_CHARS, MAX_READ_CHARS = 8_000, 40_000
MIN_READ_CHARS = 200
DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS = 20, 50
DEFAULT_CONTEXT_CHARS, MAX_CONTEXT_CHARS = 100, 400
SNIPPETS_PER_CONVERSATION = 2

ACTIVE_CONVERSATION_NOTE = (
    "This is the conversation you are in. Its current turn is not saved yet, so use your own context "
    "for anything said in this turn."
)
RUNNING_TURN_NOTE = "[... a turn is still running in this conversation; its messages are not saved yet ...]"
NO_CONVERSATIONS = "No saved conversations."
BLANK_QUERY = "Give some text to search for."


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


def _title_of(session: Session) -> str:
    return session.metadata.get("title") or "New conversation"


def make_conversation_tools(book: "ConversationBook", turn_running: Callable[[str], bool]) -> list[Callable]:
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
            f"- {session.key} | {short_time(session.metadata.get('updated_at'))} "
            f"| {len(readable_messages(session.messages))} messages | {_title_of(session)}{_marks(session)}"
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
        session = book.resolve(conversation_id)
        if session is None:
            return _unknown(conversation_id)
        lines = flatten_transcript(session.messages)
        header = [
            f"Conversation {session.key} -- {_title_of(session)} "
            f"({len(lines)} messages, last active {short_time(session.metadata.get('updated_at'))})"
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

        hits, by_terms = search(
            book.sessions(), needle, context_chars=width, snippets_per_conversation=SNIPPETS_PER_CONVERSATION
        )
        if not hits:
            return f"Nothing in the saved conversations matches {needle!r}."
        # Say so when the looser semantics produced the hits, so the model is never misled into reading
        # a same-message word match as a verbatim one.
        preamble = (
            [f"No conversation contains {needle!r} verbatim; these mention all of its words."] if by_terms else []
        )

        blocks = []
        for session, snippets in hits[:wanted]:
            blocks.append(
                f"- {session.key} | {short_time(session.metadata.get('updated_at'))} "
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
