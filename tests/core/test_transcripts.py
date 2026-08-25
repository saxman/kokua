"""Reading a stored transcript: what counts as said, how it flattens, and how it is trimmed."""

from __future__ import annotations

from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_KEY, PROVENANCE_PROACTIVE

from aimu.sessions import Session

from kokua.core.transcripts import MAX_MESSAGE_CHARS, flatten_transcript, replay_items, search, truncate_lines


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


# --- search ------------------------------------------------------------------------------------------


def _session(key, *messages):
    return Session(key=key, metadata={}, messages=list(messages))


def _search(*sessions, query):
    return search(list(sessions), query, context_chars=20, snippets_per_conversation=2)


def test_search_prefers_the_phrase_and_says_it_did_not_fall_back():
    hits, by_terms = _search(_session("a", _said("user", "the dentist appointment")), query="dentist appointment")

    assert [session.key for session, _snippets in hits] == ["a"]
    assert by_terms is False


def test_search_falls_back_to_all_terms_in_one_message_and_flags_it():
    """A caller told nothing would read a loose match as a verbatim one, so the flag is the point."""
    hits, by_terms = _search(
        _session("a", _said("user", "the appointment with my dentist")), query="dentist appointment"
    )

    assert [session.key for session, _snippets in hits] == ["a"]
    assert by_terms is True


def test_search_requires_every_term_in_the_same_message():
    hits, _by_terms = _search(
        _session("split", _said("user", "dentist"), _said("assistant", "appointment")),
        query="dentist appointment",
    )

    assert hits == []


def test_search_does_not_fall_back_for_a_single_word():
    hits, by_terms = _search(_session("a", _said("user", "nothing here")), query="dentist")

    assert hits == [] and by_terms is False


# --- replay -------------------------------------------------------------------------------------


def test_replay_items_pairs_a_tool_result_with_its_call():
    """A stored transcript splits a call from its result across two messages, joined by id.

    Concurrent dispatch appends results in completion order, so the join is by id and never by
    position: a positional join silently attributes one tool's output to another tool's call.
    """
    messages = [
        {"role": "user", "content": "read it", "timestamp": "2026-08-25T14:00:00"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_b", "function": {"name": "read_file", "arguments": '{"path": "b"}'}},
                {"id": "call_a", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "contents of a"},
        {"role": "tool", "tool_call_id": "call_b", "content": "contents of b"},
    ]
    items = replay_items(messages)
    tools = [item for item in items if item["type"] == "tool"]
    assert [t["response"] for t in tools] == ["contents of b", "contents of a"]
