"""Reading a stored transcript: what counts as said, how it flattens, and how it is trimmed."""

from __future__ import annotations

from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_KEY, PROVENANCE_PROACTIVE

from aimu.sessions import Session

from kokua.core.messages import PROVENANCE_AGENT, PROVENANCE_MIXED
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


def test_replay_items_stamps_each_user_item_with_its_transcript_index():
    """The key a turn's recorded model, effort, usage, and failure are all stored under.

    An index that misses by one lands on another turn and every one of those lookups silently
    reports another turn's figures, which is worse than reporting none.
    """
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "second"},
    ]
    users = [item for item in replay_items(messages) if item["type"] == "user"]
    assert [item["message_index"] for item in users] == [1, 3]


# The transcript a turn carrying a mid-turn message leaves behind: the turn's own message at 0, a
# tool exchange, the message the user sent into the run at 3, and the answer it produced.
_MID_TURN_TURN = [
    {"role": "user", "content": "summarize the log"},
    {"role": "assistant", "tool_calls": [{"id": "id0", "function": {"name": "read_file", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "id0", "content": "lines"},
    {"role": "user", "content": "use the cache"},
    {"role": "assistant", "content": "done"},
]


def test_replay_items_renders_a_mid_turn_message_as_an_inbox_item():
    items = replay_items(_MID_TURN_TURN, mid_turn={"0": [3]})

    assert {"type": "inbox", "text": "use the cache"} in items
    assert not any(item["type"] == "user" and item["text"] == "use the cache" for item in items)


def test_replay_items_gives_a_mid_turn_message_no_index_to_truncate_at():
    """The destructive half, and the reason the provenance record exists.

    A renderer opens a turn at the first item carrying a ``message_index`` and stamps that bubble with
    the branch and delete-from-here controls. A mid-turn message replayed as a user item therefore
    gained a control whose index cuts its *host* turn in half, deleting the answer it was sent into.
    Only the turn's own message may carry an index.
    """
    indices = [
        item["message_index"] for item in replay_items(_MID_TURN_TURN, mid_turn={"0": [3]}) if "message_index" in item
    ]

    assert indices == [0]


def test_replay_items_keeps_a_mid_turn_message_inside_its_hosts_failure_notice():
    """A mid-turn message continues the turn in progress, so it must not close that turn the way a
    message the user sent on its own does."""
    items = replay_items(_MID_TURN_TURN, mid_turn={"0": [3]}, failure={"0": "failed: out of context"})

    assert items[-1] == {"type": "notice", "text": "failed: out of context"}


def test_replay_items_renders_an_undelivered_report_naming_sender_and_selector():
    """One of the turn's own agents sent this and nobody read it, so it never became a stored
    message at all. Both the sender and the selector are named, since "undelivered" alone tells a
    reader nothing they can act on, and carries no `message_index`: it has no turn of its own."""
    items = replay_items(
        _MID_TURN_TURN,
        undelivered={"0": [{"sender": "researcher#1", "to": "assistant", "text": "look at the index"}]},
    )

    report = next(item for item in items if item["type"] == "undelivered")
    assert report["sender"] == "researcher#1"
    assert report["to"] == "assistant"
    assert report["text"] == "look at the index"
    assert "message_index" not in report


def test_replay_items_renders_one_item_per_undelivered_report():
    """A turn can carry several of these, one per agent message nothing answered to."""
    items = replay_items(
        _MID_TURN_TURN,
        undelivered={
            "0": [
                {"sender": "researcher#1", "to": "assistant", "text": "a"},
                {"sender": "researcher#2", "to": "everyone", "text": "b"},
            ]
        },
    )

    reports = [item for item in items if item["type"] == "undelivered"]
    assert [(r["sender"], r["to"], r["text"]) for r in reports] == [
        ("researcher#1", "assistant", "a"),
        ("researcher#2", "everyone", "b"),
    ]


def test_replay_items_reports_a_turns_failure_before_its_undelivered_message():
    """The order the live turn itself produces them in: `TurnRunner.reactive` sends the failure from
    inside its own `except` branch, before its `finally` closes the bus that
    `_report_undeliverable` reads afterwards. A replay that reversed the two would show a turn
    explaining itself in the opposite order from the one it was watched in."""
    items = replay_items(
        _MID_TURN_TURN,
        mid_turn={"0": [3]},
        failure={"0": "failed: out of context"},
        undelivered={"0": [{"sender": "researcher#1", "to": "assistant", "text": "look at the index"}]},
    )

    assert [item["type"] for item in items[-2:]] == ["notice", "undelivered"]


# A second, ordinary turn appended after `_MID_TURN_TURN`, so a report attached to the *first* turn
# has somewhere to flush ahead of rather than only at the end of the transcript.
_TWO_TURN_TRANSCRIPT = _MID_TURN_TURN + [
    {"role": "user", "content": "second question"},
    {"role": "assistant", "content": "second answer"},
]


def test_replay_items_flushes_an_undelivered_report_at_the_end_of_its_own_turn():
    """Discovered only when the first turn's bus closes, with nowhere earlier to attach to, so it must
    not bleed into the turn that follows: a report sitting after the second turn's own items would
    read as having happened during that turn instead of the one that actually produced it."""
    items = replay_items(
        _TWO_TURN_TRANSCRIPT,
        undelivered={"0": [{"sender": "researcher#1", "to": "assistant", "text": "look at the index"}]},
    )

    undelivered_at = next(i for i, item in enumerate(items) if item["type"] == "undelivered")
    second_turn_at = next(i for i, item in enumerate(items) if item.get("message_index") == 5)
    assert undelivered_at < second_turn_at


def test_replay_items_renders_an_unrecorded_message_as_the_turn_it_was():
    """Without a record saying otherwise, a later user message is a turn of its own: that is every
    transcript stored before a turn recorded which of its messages were sent mid-turn."""
    users = [item for item in replay_items(_MID_TURN_TURN) if item["type"] == "user"]

    assert [item["message_index"] for item in users] == [0, 3]


# The same turn, with the mid-turn message sent by one of the turn's own agents rather than typed.
_AGENT_MESSAGE_TURN = [
    *_MID_TURN_TURN[:3],
    {"role": "user", "content": "look at the index", PROVENANCE_KEY: PROVENANCE_AGENT},
    _MID_TURN_TURN[4],
]


def test_replay_items_renders_an_agent_message_from_its_tag_with_no_record_at_all():
    """The tag's own job here, which is the one thing the mid-turn record cannot do: it rides the
    message, so a reader holding the messages without the turn's metadata still cannot draw a
    worker's note as a user bubble, and cannot stamp it with the controls that would cut the host
    turn in half. The test above is the same transcript without the tag, where the record is the
    only answer and its absence costs exactly that.
    """
    items = replay_items(_AGENT_MESSAGE_TURN)

    # `from` is how a reader that signs the words (the Markdown export) says who said them, rather
    # than assuming the user did.
    assert {"type": "inbox", "text": "look at the index", "from": "agent"} in items
    assert [item["message_index"] for item in items if "message_index" in item] == [0]


def test_replay_items_leaves_the_users_own_mid_turn_message_unattributed():
    """The user's is the default case and carries no ``from``, which is what keeps the key meaning
    "not the user" wherever it appears rather than being a field every reader has to interpret."""
    items = replay_items(_MID_TURN_TURN, mid_turn={"0": [3]})

    assert {"type": "inbox", "text": "use the cache"} in items


def test_replay_items_does_not_render_an_agent_message_as_a_loop_marker():
    """The tag is in ``messages.INJECTED_USER_PROVENANCE`` because an agent's message is not a turn
    the user took, and this is why the replay cannot key on that same set: a loop marker names which
    injection the loop made, and an agent's message is not one of them."""
    items = replay_items(_AGENT_MESSAGE_TURN, mid_turn={"0": [3]})

    assert not any(item["type"] == "loop" for item in items)


def test_an_agent_message_is_not_part_of_what_was_said():
    """Neither the user nor the assistant said it, so the Markdown export and search leave it out
    rather than attributing it to whichever of them its role suggests."""
    assert flatten_transcript(_AGENT_MESSAGE_TURN) == flatten_transcript(
        [message for message in _AGENT_MESSAGE_TURN if message.get(PROVENANCE_KEY) != PROVENANCE_AGENT]
    )
    assert not any("look at the index" in line for line in flatten_transcript(_AGENT_MESSAGE_TURN))


# The same turn again, with the mid-turn message carrying both the user's own words and an agent's,
# joined into the one appended text a mixed delivery becomes.
_MIXED_MESSAGE_TURN = [
    *_MID_TURN_TURN[:3],
    {
        "role": "user",
        "content": "use the cache\n\n[message from researcher#1] and the index",
        PROVENANCE_KEY: PROVENANCE_MIXED,
    },
    _MID_TURN_TURN[4],
]


def test_replay_items_marks_a_mixed_message_mixed_rather_than_agent_or_plain_user():
    """The third state this task adds: ``PROVENANCE_AGENT`` would claim none of this message is the
    user's, which is false, so a mixed delivery cannot carry it; but leaving it with no ``from`` at
    all is what let a transcript reader sign an agent's contributed half with the user's name. Mixed
    is neither of the other two.
    """
    items = replay_items(_MIXED_MESSAGE_TURN, mid_turn={"0": [3]})

    assert {
        "type": "inbox",
        "text": "use the cache\n\n[message from researcher#1] and the index",
        "from": "mixed",
    } in items
    assert not any(item.get("from") == "agent" for item in items)


def test_a_mixed_message_is_not_part_of_what_was_said_either():
    """The same exclusion the agent-only case gets, for the same reason: this text is not cleanly the
    user's, so search must not match it as something the user said. The cost is real (the user's own
    half of this message is unsearchable too), and it is the direction under-counting is supposed to
    err in, the same one ``MessageBus.tag_for_delivery`` takes for the stored tag itself.
    """
    assert flatten_transcript(_MIXED_MESSAGE_TURN) == flatten_transcript(
        [message for message in _MIXED_MESSAGE_TURN if message.get(PROVENANCE_KEY) != PROVENANCE_MIXED]
    )
    assert not any("and the index" in line for line in flatten_transcript(_MIXED_MESSAGE_TURN))
