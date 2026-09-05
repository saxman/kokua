"""The ``conversations`` toolset: sight of the user's other conversations, their names, and their detail.

``list_conversations``, ``read_conversation`` and ``search_conversations`` let the entry agent answer
"what did we decide about X last week?" and carry context out of a past thread into a `spawn_subagent`
task. Those three read what was *said*. ``export_conversation`` is for the other question, how a
conversation *ran*: it writes the Markdown export, which keeps the reasoning, the tool calls with their
arguments and results, the sub-agent activity, and the per-turn cost that the reading tools deliberately
drop. ``rename_conversation`` writes exactly one field, what a conversation is called in the sidebar.
All five are entry-agent-only, like the other cross-cutting tools: a worker shares no history and has no
conversation identity, so "the user's other conversations" only means something to the agent the user is
talking to.

Two of the five write, and it is worth being precise about what: a rename changes a conversation's
title, and an export changes nothing about any conversation, only leaving a file under
``downloads_path`` named for the conversation it rendered. Nothing here can write a message, a
transcript, or a path the model chose.

Everything here is presentation. The transcript reading, flattening, and searching are in
``core/transcripts.py``, the export rendering is ``transcript_export.render_markdown``, and resolving an
id or a unique prefix is ``ConversationBook.resolve``. What is left in this module is the tool schemas,
the bounds a model may pass and how they are clamped, and the sentences it reads back -- including the
two markers that keep a stored snapshot honest.

``ToolsetContext`` carries the live ``ConversationBook``, the assistant's ``turn_running``, and its
``schedule_rename`` off ``LiveState``, which is what lets this module build the tools without importing
``core.assistant``; the export's directory comes off ``ctx.config`` instead, since it is a path the
config derives rather than live state. ``schedule_rename`` is not a convenience: a rename cannot be
written inline from a tool, because the write takes the turn slot the calling turn is already holding,
and ``Assistant.schedule_rename`` is what turns that deadlock into a write queued behind the turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from aimu.sessions import Session
from aimu.tools import tool

from kokua.core.transcripts import flatten_transcript, readable_messages, search, short_time, truncate_lines
from kokua.registry.registry import Toolset
from kokua.transcript_export import DEFAULT_MAX_PAYLOAD_CHARS, render_markdown

if TYPE_CHECKING:
    # Annotation only. A real import would run kokua/core/__init__.py, and this module is reached from
    # core/agents.py (via entry-point discovery) which core/build.py also imports -- the cycle
    # core/build.py and core/assistant.py already dodge with deferred imports. LiveState leaves the same
    # field untyped for the same reason.
    from kokua.core.conversations import ConversationBook

DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT = 30, 200
DEFAULT_READ_CHARS, MAX_READ_CHARS = 8_000, 40_000
MIN_READ_CHARS = 200
DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS = 20, 50
DEFAULT_CONTEXT_CHARS, MAX_CONTEXT_CHARS = 100, 400
SNIPPETS_PER_CONVERSATION = 2

# The same fact about the same conversation, said to the two agents that can be reading it. Which one
# applies is not cosmetic: the entry agent is *in* the active conversation and holds this turn in its
# context, and a worker is in none of them and holds nothing, so telling a worker to fall back on its
# own context would send it to a place with no transcript in it. A worker spawned to evaluate the
# conversation the user is sitting in is the likeliest case of all, which is why the wrong wording here
# would misfire on the common path rather than an edge.
ACTIVE_CONVERSATION_NOTE = (
    "This is the conversation you are in. Its current turn is not saved yet, so use your own context "
    "for anything said in this turn."
)
DELEGATING_CONVERSATION_NOTE = (
    "This is the conversation that delegated to you. The turn that spawned you is not saved yet, so "
    "what you can read here stops just before it; the task you were given is what you have of that turn."
)
RUNNING_TURN_NOTE = "[... a turn is still running in this conversation; its messages are not saved yet ...]"
NO_CONVERSATIONS = "No saved conversations."
BLANK_QUERY = "Give some text to search for."
BLANK_TITLE = "Give a title to rename the conversation to. An empty title would leave it unnamed."

# Said on every rename, because the model would otherwise have no way to know. The write is queued
# behind the turn making it (see ``Assistant.schedule_rename``), so a ``read_conversation`` or
# ``list_conversations`` later in this same turn still reports the old name, and a model that took
# silence for success would "correct" itself in a second call.
RENAME_DEFERRED_NOTE = "It takes effect when this turn finishes, so it will still read as the old title until then."

# Above this many lines, an export is a file the model should hand onward rather than read. The number
# is a judgment about context rather than about the file: a few hundred lines of transcript is already
# thousands of tokens, and AIMU's `read_file` truncates from the top and takes no offset, so a model
# that starts reading a long export cannot page to the part it wanted (see TODO item 19). Deliberately
# not a config setting: it advises, and the model is free to read the file anyway.
DELEGATE_ABOVE_LINES = 400

EXPORT_CONTENTS_NOTE = (
    "The file holds the whole record: what was said, the reasoning behind each answer, every tool call "
    "with its arguments and its result, any sub-agent activity, each turn's model and reasoning effort, "
    "what it cost, and why a turn stopped if it stopped short."
)
LARGE_EXPORT_NOTE = (
    "This file is long. If you can delegate to a sub-agent that reads files, give it this path and the "
    "question instead of reading the file here: reading it yourself would spend this conversation's "
    "context on it, and a single read cannot cover a file this size anyway."
)


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


def make_conversation_tools(
    book: "ConversationBook",
    turn_running: Callable[[str], bool],
    schedule_rename: Callable[[str, str], None],
    downloads_path: Path,
    is_entry_agent: bool,
) -> list[Callable]:
    """Build the cross-conversation tools bound to the live ``ConversationBook``.

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

    ``rename_conversation`` is the exception to all of that, and takes nothing from the book but the id
    resolution: the write itself is handed to ``schedule_rename`` rather than made here, because a tool
    holds its turn's gate slot and the write needs the same one (invariant 1 in ``core/turns.py``).

    ``export_conversation`` reads the store like the rest and writes its file inside
    ``downloads_path``, under a name taken from the resolved session's key. Passing the directory in
    rather than reading it off the config keeps the one thing this factory writes outside the store
    visible in its signature, and the name coming from the store rather than from the model is what
    makes a path the model chose unreachable: there is no argument here that reaches the filesystem.

    ``is_entry_agent`` decides which sentence the active conversation is described with, and it is the
    only thing in here that differs between two agents holding the same capability. The entry agent is
    in that conversation; a worker holding this toolset (the shipped ``introspector``) was spawned *by*
    it and holds none of its transcript, so the two need statements that are each true of one of them.
    A flag rather than two toolsets because everything else these tools do is identical, and a flag
    rather than a docstring per agent because a tool schema is a literal a reader can find.
    """

    def _unknown(conversation_id: str) -> str:
        return (
            f"No conversation matches {conversation_id!r}. "
            "Call `list_conversations` or `search_conversations` for current ids."
        )

    def _marks(session: Session) -> str:
        marks = " (current)" if session.key == book.active_id else ""
        return marks + (" (turn in progress)" if turn_running(session.key) else "")

    def _active_note(session: Session) -> list[str]:
        """The note for the conversation the user is in, in the words true of whoever is reading it.

        A list rather than an optional string so both callers can splice it into the lines they are
        already assembling, and so "no note at all" needs no branch at the call site.
        """
        if session.key != book.active_id:
            return []
        return [ACTIVE_CONVERSATION_NOTE if is_entry_agent else DELEGATING_CONVERSATION_NOTE]

    @tool
    async def list_conversations(limit: int = DEFAULT_LIST_LIMIT) -> str:
        """List the user's saved chat conversations, most recently active first.

        Each line gives the conversation id, when it was last active, how many messages it holds, and its
        title (derived from its first user message). "(current)" marks the conversation the user is in
        right now: if you are the agent talking to them, its transcript is already in your context and
        there is no need to read it back, and if you were delegated a task about it, read it like any
        other. One marked "(turn in progress)" has a reply still being generated, so its saved
        transcript stops short of that turn. Pass an id to `read_conversation`, or use
        `search_conversations` to find one by what was said in it. These are chat threads, not scheduled
        tasks; see `list_scheduled_tasks` for those.

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
        header.extend(_active_note(session))
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

    @tool
    async def rename_conversation(conversation_id: str, title: str) -> str:
        """Rename one saved conversation, changing what it is called in the sidebar and nothing else.

        Give an id from `list_conversations` or `search_conversations` (a unique leading fragment of at
        least six characters also works). Keep the title short, at most about six words: it is read in a
        narrow sidebar, and a longer one is cut off. Anything past 40 characters is dropped.

        The rename is applied when this turn finishes, not immediately, so reading the conversation
        again in this same turn will still show the old title. Do not call this a second time to
        "fix" that.

        Args:
            conversation_id: The conversation to rename (full id, or a unique leading fragment).
            title: The new title.
        """
        session = book.resolve(conversation_id)
        if session is None:
            return _unknown(conversation_id)
        if not title.strip():
            return BLANK_TITLE
        was = _title_of(session)
        schedule_rename(session.key, title)
        return f"Renaming {session.key} from {was!r} to {title!r}. {RENAME_DEFERRED_NOTE}"

    @tool
    async def export_conversation(conversation_id: str, full: bool = False) -> str:
        """Write one saved conversation to a Markdown file, including everything `read_conversation` drops.

        Use this when the question is how a conversation *ran* rather than what was said in it: why a
        turn failed, which tools were called and what they returned, what the reasoning was, what a
        sub-agent was asked and what it answered, or what a turn cost. Give an id from
        `list_conversations` or `search_conversations` (a unique leading fragment of at least six
        characters also works).

        This changes nothing about the conversation. The file is named for the conversation, so
        exporting the same one again replaces that file rather than leaving two. The answer says how
        long the file is: when it is long, hand the path to a sub-agent that can read files together
        with the question you want answered, instead of reading the file into this conversation.

        Args:
            conversation_id: The conversation to export (full id, or a unique leading fragment).
            full: Keep every tool call's arguments and result whole. Off by default, which cuts any one
                of them past a few thousand characters and says so where it cut. Turn it on when the
                exact text of a long tool result is what you are trying to read.
        """
        session = book.resolve(conversation_id)
        if session is None:
            return _unknown(conversation_id)
        markdown = render_markdown(session, max_payload_chars=None if full else DEFAULT_MAX_PAYLOAD_CHARS)
        # The web front end's download route serves this directory and 404s rather than creating it,
        # so a fresh $KOKUA_HOME may never have had anything written here.
        downloads_path.mkdir(parents=True, exist_ok=True)
        destination = downloads_path / f"{session.key}.md"
        destination.write_text(markdown, encoding="utf-8")

        # Lines, because that is the unit `read_file` caps by, and the file's real byte size, because
        # that is what a reader comparing it against a context window needs.
        lines = len(markdown.splitlines())
        kilobytes = len(markdown.encode("utf-8")) / 1024
        answer = [
            f"Exported {session.key} -- {_title_of(session)} "
            f"({len(readable_messages(session.messages))} messages, "
            f"last active {short_time(session.metadata.get('updated_at'))}).",
            f"File: {destination} ({lines} lines, {kilobytes:.1f} KB)",
            EXPORT_CONTENTS_NOTE,
        ]
        if lines > DELEGATE_ABOVE_LINES:
            answer.append(LARGE_EXPORT_NOTE)
        answer.extend(_active_note(session))
        # Last, because the turn it is talking about is newer than anything in the file.
        if turn_running(session.key):
            answer.append(RUNNING_TURN_NOTE)
        return "\n".join(answer)

    return [list_conversations, read_conversation, search_conversations, rename_conversation, export_conversation]


CONVERSATIONS_GUIDANCE = (
    " You can see across the user's other chat conversations with `list_conversations`, "
    "`read_conversation`, and `search_conversations`, which read their saved transcripts. This turn is "
    "not saved yet, so use your own context for the conversation you are in. Those three show what was "
    "said; when you are asked about how a conversation ran, which tools it called, what they returned, "
    "or why a turn failed, use `export_conversation`, which writes all of that to a file and gives you "
    "the path. `rename_conversation` is the only one that changes a conversation, and all it changes is "
    "the title."
)

TOOLSET = Toolset(
    name="conversations",
    description="Visibility across the user's other conversations: reading, renaming, and exporting one in full.",
    build=lambda ctx: make_conversation_tools(
        ctx.state.conversation_book,
        ctx.state.turn_running,
        ctx.state.schedule_rename,
        ctx.config.downloads_path,
        ctx.agent_name == ctx.config.entry_agent,
    ),
    guidance=CONVERSATIONS_GUIDANCE,
    cross_cutting=True,
)
