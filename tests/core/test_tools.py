"""The supervisor's read-only cross-conversation tools: flattening, bounding, and the honesty markers.

The pure helpers are tested directly; the tools are built over a real ``TinyDBSessionStore`` and
``ConversationBook`` (no agents, no model) and called as plain coroutines.
"""

from __future__ import annotations

import asyncio

from aimu.aio.channels.base import ChannelMessage
from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.sessions import Session, TinyDBSessionStore

from kokua.core.assistant import Assistant
from kokua.core.tools import (
    ACTIVE_CONVERSATION_NOTE,
    BLANK_QUERY,
    MAX_CONTEXT_CHARS,
    MAX_MESSAGE_CHARS,
    NO_CONVERSATIONS,
    RUNNING_TURN_NOTE,
    flatten_transcript,
    make_conversation_tools,
    truncate_lines,
)
from kokua.core.conversations import ConversationBook
from kokua.core.turn_gate import TurnGate
from tests.channels import FakeChannel, _config
from tests.helpers import MockAsyncModelClient


def _session(key: str, *, title=None, updated_at="2026-08-11T09:14:00", messages=None) -> Session:
    metadata = {"updated_at": updated_at, "created_at": updated_at}
    if title:
        metadata["title"] = title
    return Session(key=key, metadata=metadata, messages=list(messages or []))


def _book(tmp_path, *sessions: Session, adopt=True) -> ConversationBook:
    config = _config(tmp_path)
    store = TinyDBSessionStore(str(config.sessions_path))
    for session in sessions:
        store.save(session)
    book = ConversationBook(store, TurnGate(lambda cid: asyncio.Lock()), config, on_active_change=lambda _cid: None)
    if adopt:
        book.adopt_most_recent()  # sets the active pointer without building an agent
    return book


def _tools(book: ConversationBook, running=()) -> dict:
    tools = make_conversation_tools(book, lambda conversation_id: conversation_id in running)
    return {fn.__name__: fn for fn in tools}


def _said(role: str, text, **extra) -> dict:
    return {"role": role, "content": text, **extra}


# --- flattening ------------------------------------------------------------------------------------


def test_flatten_keeps_only_what_was_said():
    lines = flatten_transcript(
        [
            _said("system", "you are a lean supervisor"),
            _said("user", "how is the weather"),
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "web_search"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "sunny, 21C"},
            _said("assistant", "It is sunny.", thinking="let me check the search result"),
        ]
    )
    assert lines == ["user: how is the weather", "assistant: It is sunny."]
    joined = "\n".join(lines)
    assert "web_search" not in joined
    assert "lean supervisor" not in joined
    assert "let me check" not in joined


def test_flatten_prefixes_the_message_time_when_present():
    stamped = flatten_transcript([_said("user", "hi", timestamp="2026-08-11T09:14:31.123456")])
    assert stamped == ["[2026-08-11 09:14] user: hi"]
    assert flatten_transcript([_said("user", "hi")]) == ["user: hi"]


def test_flatten_skips_injected_turns_and_labels_proactive():
    lines = flatten_transcript(
        [
            _said("user", "continue", **{PROVENANCE_KEY: PROVENANCE_CONTINUATION}),
            _said("assistant", "Your flight leaves at 8.", **{PROVENANCE_KEY: PROVENANCE_PROACTIVE}),
        ]
    )
    assert lines == ["assistant (proactive): Your flight leaves at 8."]


def test_flatten_replaces_image_blocks_with_a_placeholder():
    lines = flatten_transcript(
        [
            _said(
                "user",
                [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "/images/ab.png"}},
                ],
            )
        ]
    )
    assert lines == ["user: look [image]"]
    assert "/images/ab.png" not in lines[0]


def test_flatten_cuts_one_oversized_message():
    (line,) = flatten_transcript([_said("user", "x" * 5000)])
    assert "[message truncated, 5000 chars total]" in line
    assert len(line) < MAX_MESSAGE_CHARS + 100


def test_truncate_lines_drops_oldest_and_counts_them():
    lines = [f"line {n}" for n in range(20)]
    kept, dropped = truncate_lines(lines, 30)
    assert kept == lines[len(lines) - len(kept) :]  # a suffix: the newest entries
    assert sum(len(line) + 1 for line in kept) <= 30
    assert dropped == len(lines) - len(kept)


def test_truncate_lines_keeps_at_least_the_newest_line():
    lines = ["old", "newest"]
    assert truncate_lines(lines, 1) == (["newest"], 1)


# --- list_conversations ----------------------------------------------------------------------------


async def test_list_newest_first_with_counts_and_marks(tmp_path):
    book = _book(
        tmp_path,
        _session("aaaaaaaa1", title="Oldest", updated_at="2026-08-09T08:00:00"),
        _session(
            "bbbbbbbb2",
            title="Middle",
            updated_at="2026-08-10T08:00:00",
            messages=[_said("user", "hi"), _said("assistant", "hello")],
        ),
        _session("cccccccc3", title="Newest", updated_at="2026-08-11T08:00:00"),
    )
    output = await _tools(book, running={"aaaaaaaa1"})["list_conversations"]()
    lines = output.splitlines()

    assert [line.split()[1] for line in lines] == ["cccccccc3", "bbbbbbbb2", "aaaaaaaa1"]
    assert "2 messages" in lines[1]
    assert "(current)" in lines[0] and book.active_id == "cccccccc3"  # newest is adopted at startup
    assert "(current)" not in lines[1] and "(current)" not in lines[2]
    assert "(turn in progress)" in lines[2]
    assert "2026-08-11 08:00" in lines[0]


async def test_list_on_an_empty_store(tmp_path):
    assert await _tools(_book(tmp_path, adopt=False))["list_conversations"]() == NO_CONVERSATIONS


async def test_list_limit_clamped_and_reports_hidden(tmp_path):
    book = _book(tmp_path, *(_session(f"c{n}", updated_at=f"2026-08-1{n}T08:00:00") for n in range(5)))
    list_conversations = _tools(book)["list_conversations"]

    output = await list_conversations(limit=2)
    assert len(output.splitlines()) == 3
    assert "3 older conversations not shown" in output

    for unusable in (0, -1, "nope", None):
        assert len((await list_conversations(limit=unusable)).splitlines()) == 5


async def test_list_untitled_conversation_shows_the_placeholder(tmp_path):
    output = await _tools(_book(tmp_path, _session("c1")))["list_conversations"]()
    assert "New conversation" in output


# --- read_conversation -----------------------------------------------------------------------------


async def test_read_returns_the_ordered_transcript(tmp_path):
    book = _book(
        tmp_path,
        _session(
            "target01",
            title="Kauai trip",
            updated_at="2026-08-09T08:00:00",
            messages=[
                _said("system", "guidance"),
                _said("user", "plan my trip to Kauai"),
                {"role": "tool", "tool_call_id": "c1", "content": "flight data"},
                _said("assistant", "Here is an itinerary."),
            ],
        ),
        _session("other002", updated_at="2026-08-11T08:00:00"),
    )
    output = await _tools(book)["read_conversation"]("target01")

    assert output.splitlines()[0] == "Conversation target01 -- Kauai trip (2 messages, last active 2026-08-09 08:00)"
    assert output.index("plan my trip") < output.index("Here is an itinerary")
    assert "guidance" not in output and "flight data" not in output
    assert ACTIVE_CONVERSATION_NOTE not in output


async def test_read_truncates_the_oldest_and_marks_it(tmp_path):
    messages = [_said("user" if n % 2 == 0 else "assistant", f"message number {n}") for n in range(50)]
    book = _book(tmp_path, _session("c1", messages=messages))
    output = await _tools(book)["read_conversation"]("c1", max_chars=300)

    assert "older messages omitted to fit max_chars" in output
    assert output.rstrip().endswith("message number 49")
    assert "message number 0" not in output


async def test_read_unknown_id_reports_and_does_not_resurrect_it(tmp_path):
    book = _book(tmp_path, _session("c1"))
    output = await _tools(book)["read_conversation"]("deleted-one")

    assert "No conversation matches" in output and "list_conversations" in output
    assert book._store.list_keys() == ["c1"]  # a store.get() would have created a blank session


async def test_read_accepts_a_unique_id_prefix(tmp_path):
    book = _book(tmp_path, _session("abcdef123456", messages=[_said("user", "the parakeet is named Dexter")]))
    assert "Dexter" in await _tools(book)["read_conversation"]("abcdef")


async def test_read_rejects_an_ambiguous_prefix(tmp_path):
    book = _book(tmp_path, _session("abcdef111"), _session("abcdef222"))
    assert "No conversation matches" in await _tools(book)["read_conversation"]("abcdef")


async def test_read_rejects_a_too_short_prefix(tmp_path):
    book = _book(tmp_path, _session("abcdef123456"))
    assert "No conversation matches" in await _tools(book)["read_conversation"]("abcd")


async def test_read_of_the_active_conversation_warns_the_current_turn_is_unsaved(tmp_path):
    book = _book(
        tmp_path,
        _session("active01", updated_at="2026-08-11T08:00:00", messages=[_said("user", "hi")]),
        _session("older002", updated_at="2026-08-09T08:00:00", messages=[_said("user", "hi")]),
    )
    read_conversation = _tools(book)["read_conversation"]
    assert ACTIVE_CONVERSATION_NOTE in await read_conversation("active01")
    assert ACTIVE_CONVERSATION_NOTE not in await read_conversation("older002")


async def test_read_flags_a_running_turn_as_the_last_line(tmp_path):
    book = _book(tmp_path, _session("c1", messages=[_said("user", "hi")]))
    output = await _tools(book, running={"c1"})["read_conversation"]("c1")
    # Last, because that turn is newer than everything shown.
    assert output.rstrip().endswith(RUNNING_TURN_NOTE)


async def test_read_of_an_empty_conversation(tmp_path):
    output = await _tools(_book(tmp_path, _session("c1")))["read_conversation"]("c1")
    assert output.endswith("(no messages)")
    assert "0 messages" in output


async def test_read_never_builds_an_agent(tmp_path):
    """The store is the only source. A book with no registry bound would raise on any agent path, so this
    pins the rule mechanically rather than by comment."""
    book = _book(tmp_path, _session("c1", messages=[_said("user", "hi")]))
    assert book._registry is None
    assert "hi" in await _tools(book)["read_conversation"]("c1")


# --- search_conversations --------------------------------------------------------------------------


async def test_search_returns_ids_titles_and_snippets(tmp_path):
    book = _book(
        tmp_path,
        _session(
            "match001",
            title="Island plans",
            updated_at="2026-08-09T08:00:00",
            messages=[
                _said("user", f"{'preamble ' * 40}we booked eight nights on Kauai{' and more ' * 40}"),
            ],
        ),
        _session("other002", updated_at="2026-08-11T08:00:00", messages=[_said("user", "unrelated")]),
    )
    output = await _tools(book)["search_conversations"]("kauai")

    assert "match001" in output and "Island plans" in output
    assert "other002" not in output
    snippet = output.splitlines()[1].strip()
    assert "eight nights on Kauai and more" in snippet
    # Ellipsed at both ends, because the match sits in the middle of a longer message.
    assert snippet.startswith("user: ...") and snippet.endswith("...")


async def test_search_falls_back_to_all_terms_when_the_phrase_misses(tmp_path):
    book = _book(tmp_path, _session("c1", messages=[_said("user", "I moved the dentist to Tuesday afternoon")]))
    search = _tools(book)["search_conversations"]

    fallback = await search("dentist tuesday")
    assert "c1" in fallback
    assert "verbatim" in fallback

    direct = await search("dentist to Tuesday")
    assert "c1" in direct
    assert "verbatim" not in direct


async def test_search_all_terms_must_be_in_one_message(tmp_path):
    """Deliberate: a conversation mentioning one term early and another much later is almost never the
    one being looked for."""
    book = _book(tmp_path, _session("c1", messages=[_said("user", "dentist"), _said("assistant", "tuesday")]))
    assert "c1" not in await _tools(book)["search_conversations"]("dentist tuesday")


async def test_search_bounds_snippets_and_results(tmp_path):
    repetitive = _session("c1", messages=[_said("user", f"kauai note {n}") for n in range(10)])
    assert len((await _tools(_book(tmp_path, repetitive))["search_conversations"]("kauai")).splitlines()) == 3

    many = [
        _session(f"conv{n}", updated_at=f"2026-08-0{n}T08:00:00", messages=[_said("user", "kauai")])
        for n in range(1, 9)
    ]
    output = await _tools(_book(tmp_path / "many", *many))["search_conversations"]("kauai", max_results=3)
    assert output.count("| kauai") == 0  # the title is the derived one, not the needle
    assert len([line for line in output.splitlines() if line.startswith("- ")]) == 3
    assert "5 more matching conversations not shown" in output


async def test_search_ignores_tool_output_and_the_system_message(tmp_path):
    book = _book(
        tmp_path,
        _session(
            "c1",
            messages=[
                _said("system", "you may search kauai"),
                {"role": "tool", "tool_call_id": "t1", "content": "kauai weather: sunny"},
                _said("user", "unrelated"),
            ],
        ),
    )
    assert "Nothing in the saved conversations matches" in await _tools(book)["search_conversations"]("kauai")


async def test_search_blank_query(tmp_path):
    assert await _tools(_book(tmp_path, _session("c1")))["search_conversations"]("   ") == BLANK_QUERY


async def test_search_context_chars_clamped(tmp_path):
    book = _book(tmp_path, _session("c1", messages=[_said("user", f"{'a ' * 900}kauai{' b' * 900}")]))
    output = await _tools(book)["search_conversations"]("kauai", context_chars=100_000)
    snippet = output.splitlines()[1]
    assert len(snippet) < 2 * MAX_CONTEXT_CHARS + 100


# --- the wired tools, over the real book -----------------------------------------------------------


async def test_search_and_read_agree_through_the_wired_tools(tmp_path):
    """End to end on the instances the agent actually holds: a past conversation is findable by its text
    and readable by the id search returned."""
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["noted"])
    )
    await assistant._handle(
        ChannelMessage(text="the parakeet is named Dexter", channel="fake"), conversation_id=assistant._active_id
    )
    past_id = assistant._active_id
    await assistant.new_conversation()

    tools = {getattr(fn, "__name__", None): fn for fn in assistant._agent.tools}
    found = await tools["search_conversations"]("parakeet")
    assert past_id in found
    assert past_id != assistant._active_id

    transcript = await tools["read_conversation"](past_id)
    assert "Dexter" in transcript
    assert ACTIVE_CONVERSATION_NOTE not in transcript
    assert assistant._active_id in await tools["list_conversations"]()
