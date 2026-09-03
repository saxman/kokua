"""Mock-only tests for the web front end (WebChannel + Starlette server)."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.helpers import BlockingModelClient, MockAsyncModelClient
from kokua.channels.web import WebChannel
from kokua.core.transcripts import SPAWN_SUBAGENT_TOOL_NAME
from kokua.core.transcripts import replay_items
from kokua.config import AssistantConfig
from tests.channels import example_agents, planning_settings
from kokua.frontends.web import build_app

from aimu.aio.channels.base import ChannelMessage
from aimu.models import (
    PROVENANCE_CONTINUATION,
    PROVENANCE_FINAL_ANSWER,
    PROVENANCE_KEY,
    PROVENANCE_PROACTIVE,
    StreamChunk,
    StreamingContentType,
)


class _FakeWS:
    """Captures send_json frames; stands in for a Starlette WebSocket in unit tests."""

    def __init__(self):
        self.frames = []
        self.closed = 0

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self):
        self.closed += 1


def _config(tmp_path, **overrides) -> AssistantConfig:
    base = {
        "data_dir": tmp_path,
        "agents": example_agents(),
        "entry_agent": "assistant",
        # A resolved config always carries the declared [planning] section; a planned turn through this
        # config reads its settings the way a real one does.
        "toolset_settings": planning_settings(),
    }
    base.update(overrides)
    return AssistantConfig(**base)


# --- WebChannel unit tests -------------------------------------------------------------------


async def test_web_channel_send_str_flags_proactive():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send("hello")  # no reply_to -> proactive
    await channel.send("hi back", reply_to=ChannelMessage(text="x"))
    assert ws.frames[0] == {"type": "message", "text": "hello", "proactive": True}
    assert ws.frames[1] == {"type": "message", "text": "hi back", "proactive": False}


async def test_web_channel_send_stream_emits_tokens_then_done():
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "a")
        yield StreamChunk(StreamingContentType.GENERATING, "b")

    await channel.send(gen())
    assert ws.frames == [
        {"type": "token", "text": "a"},
        {"type": "token", "text": "b"},
        {"type": "done"},
    ]


async def test_web_channel_emits_thinking_and_tool_frames_by_default():
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "hmm")
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 2}})
        yield StreamChunk(StreamingContentType.GENERATING, "4")

    await channel.send(gen())
    assert ws.frames == [
        {"type": "thinking", "text": "hmm"},
        {"type": "tool", "name": "calc", "arguments": {"x": 2}, "response": None},
        {"type": "token", "text": "4"},
        {"type": "done"},
    ]


async def test_web_channel_send_suppresses_spawn_subagent_tool_frame():
    """The spawn's own `subagent` card already shows role/task/result, so the parent's `tool` frame
    for spawn_subagent specifically is dropped -- it would otherwise arrive after the card (AIMU emits
    TOOL_CALLING only once the tool returns) and duplicate what the card already shows."""
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": SPAWN_SUBAGENT_TOOL_NAME, "arguments": {}})
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 2}})

    await channel.send(gen())
    assert ws.frames == [
        {"type": "tool", "name": "calc", "arguments": {"x": 2}, "response": None},
        {"type": "done"},
    ]


async def test_web_channel_send_relays_an_injected_round_with_the_prompt_that_was_sent():
    """The base channel maps the CONTINUING chunk; Kokua's override only has to stop swallowing it.
    The wrap-up's wording is the case that matters: a marker showing the nudge's text here would say
    the model was told to keep working when it was told to stop."""
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "a")
        yield StreamChunk(
            StreamingContentType.CONTINUING,
            {"kind": "final_answer", "prompt": "You have reached the tool-use limit."},
        )
        yield StreamChunk(StreamingContentType.GENERATING, "b")

    await channel.send(gen())
    assert ws.frames == [
        {"type": "token", "text": "a"},
        {"type": "loop", "reason": "final_answer", "text": "You have reached the tool-use limit."},
        {"type": "token", "text": "b"},
        {"type": "done"},
    ]


async def test_web_channel_send_no_longer_marks_a_bare_iteration_rise():
    """A tool round raises the counter and injects nothing. Replay never drew a marker there, so
    drawing one live made a turn read differently after a reload than it did while it arrived."""
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "a", iteration=0)
        yield StreamChunk(StreamingContentType.GENERATING, "b", iteration=1)

    await channel.send(gen())
    assert [f["type"] for f in ws.frames] == ["token", "token", "done"]


async def test_web_channel_skips_empty_generating_chunks():
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "")
        yield StreamChunk(StreamingContentType.GENERATING, "hi")
        yield StreamChunk(StreamingContentType.GENERATING, "")

    await channel.send(gen())
    assert ws.frames == [{"type": "token", "text": "hi"}, {"type": "done"}]


async def test_web_channel_send_conversations_emits_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    items = [{"id": "a1", "title": "Trip plan", "active": True}]
    await channel.send_conversations(items)
    assert ws.frames == [{"type": "conversations", "items": items}]


async def test_web_channel_send_turn_saved_emits_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_turn_saved("abc123", 4)
    assert ws.frames == [{"type": "turn_saved", "conversation_id": "abc123", "message_index": 4}]


async def test_web_channel_send_settings_emits_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    values = {"planning.plan_review": True, "planning.show_reasoning": False}
    await channel.send_settings(values)
    assert ws.frames == [{"type": "settings", "values": values}]


async def test_web_channel_stream_activity_shows_an_injected_round_and_withholds_the_answer():
    """`stream_activity` maps chunks itself rather than reusing the base loop, so the branch has to
    exist in both places or a planned turn shows no boundary while an ordinary one does."""
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "pondering")
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 1}})
        yield StreamChunk(StreamingContentType.CONTINUING, {"kind": "continuation", "prompt": "Keep going."})
        yield StreamChunk(StreamingContentType.GENERATING, "the ")
        yield StreamChunk(StreamingContentType.GENERATING, "answer")

    answer = await channel.stream_activity(gen())
    assert answer == "the answer"  # accumulated, not streamed
    assert {"type": "loop", "reason": "continuation", "text": "Keep going."} in ws.frames
    types = [f["type"] for f in ws.frames]
    assert "thinking" in types and "tool" in types
    assert "token" not in types and "done" not in types  # answer withheld, no terminator


async def test_web_channel_stream_activity_types_a_missing_kind_as_a_string():
    """A `loop` frame's `reason` is documented as a string, so a chunk missing `kind` must not put a
    null there. The same default as AIMU's base `send` arm, so the two paths agree on the shape."""
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.CONTINUING, {"prompt": "Keep going."})

    await channel.stream_activity(gen())
    assert {"type": "loop", "reason": "", "text": "Keep going."} in ws.frames


async def test_web_channel_stream_activity_show_answer_emits_tokens():
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "hmm")
        yield StreamChunk(StreamingContentType.GENERATING, "the ")
        yield StreamChunk(StreamingContentType.GENERATING, "answer")

    text = await channel.stream_activity(gen(), show_answer=True)
    assert text == "the answer"  # still captured
    types = [f["type"] for f in ws.frames]
    assert types.count("token") == 2 and "thinking" in types  # answer shown live (verbose)
    assert "done" not in types  # no terminator; caller ends the turn


async def test_web_channel_stream_activity_suppresses_spawn_subagent_tool_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": SPAWN_SUBAGENT_TOOL_NAME, "arguments": {}})
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 2}})

    await channel.stream_activity(gen())
    assert ws.frames == [{"type": "tool", "name": "calc", "arguments": {"x": 2}, "response": None}]


async def test_web_channel_stream_activity_tool_frame_carries_the_response():
    """`stream_activity` maps chunks to frames itself rather than reusing the base loop, so the result
    has to be carried on this path too or a planned turn's trace shows calls without their output."""
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 2}, "response": "4"})

    await channel.stream_activity(gen())
    assert ws.frames == [{"type": "tool", "name": "calc", "arguments": {"x": 2}, "response": "4"}]


async def test_web_channel_send_phase_and_done():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_phase("Planner", "drafting a plan")
    await channel.send_done()
    assert ws.frames == [
        {"type": "phase", "label": "Planner", "detail": "drafting a plan"},
        {"type": "done"},
    ]


async def test_web_channel_send_subagent_emits_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_subagent({"id": "plan-review-0", "role": "Plan reviewer", "status": "running", "round": 0})
    assert ws.frames == [
        {"type": "subagent", "id": "plan-review-0", "role": "Plan reviewer", "status": "running", "round": 0}
    ]


def test_replay_items_interleaves_subagent_after_user():
    messages = [{"role": "user", "content": "do X"}, {"role": "assistant", "content": "done"}]
    subagent = {"0": [{"role": "Plan reviewer", "status": "rejected", "issues": ["x"], "round": 0}]}
    items = replay_items(messages, subagent=subagent)
    # This test is about the subagent card landing right after the user bubble, not about the
    # bubble's own shape, so it checks type and text rather than the whole dict (which now also
    # carries a message_index this test has no stake in).
    assert items[0]["type"] == "user" and items[0]["text"] == "do X"
    assert items[1]["type"] == "subagent" and items[1]["status"] == "rejected"
    assert items[2]["type"] == "message"


def test_replay_items_threads_message_timestamp():
    messages = [
        {"role": "user", "content": "do X", "timestamp": "2026-07-23T15:45:00"},
        {"role": "assistant", "content": "done", "timestamp": "2026-07-23T15:45:07"},
    ]
    items = replay_items(messages)
    user_item = next(i for i in items if i["type"] == "user")
    message_item = next(i for i in items if i["type"] == "message")
    assert user_item["ts"] == "2026-07-23T15:45:00"
    assert message_item["ts"] == "2026-07-23T15:45:07"


def test_replay_items_omits_ts_when_message_has_no_timestamp():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    items = replay_items(messages)
    assert all("ts" not in i for i in items)  # legacy messages (no timestamp) render no caption


def test_replay_items_subagent_inherits_turn_timestamp():
    messages = [
        {"role": "user", "content": "do X", "timestamp": "2026-07-23T15:45:00"},
        {"role": "assistant", "content": "done", "timestamp": "2026-07-23T15:45:07"},
    ]
    subagent = {"0": [{"role": "Plan reviewer", "status": "rejected", "issues": ["x"], "round": 0}]}
    items = replay_items(messages, subagent=subagent)
    subagent_item = next(i for i in items if i["type"] == "subagent")
    assert subagent_item["ts"] == "2026-07-23T15:45:00"  # inherits its turn's user-message timestamp


def test_replay_items_omits_subagent_by_default():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    items = replay_items(messages)
    assert not any(i["type"] == "subagent" for i in items)


def test_replay_items_replays_verbose_trace_not_committed_answer():
    # A verbose turn persists its raw trace; reload replays phase + reasoning items and must NOT also
    # emit the committed assistant message (the trace's last Executor phase already holds the answer).
    messages = [{"role": "user", "content": "do X"}, {"role": "assistant", "content": "THE ANSWER"}]
    trace = {
        "0": [
            {"label": "Planner", "detail": "drafting a plan", "text": "THE PLAN"},
            {"label": "Plan reviewer", "detail": "round 1", "text": "looks good"},
            {"label": "Executor", "detail": "carrying out the plan", "text": "THE ANSWER"},
        ]
    }
    items = replay_items(messages, trace=trace)
    assert items == [
        {"type": "user", "text": "do X", "message_index": 0},
        {"type": "phase", "label": "Planner", "detail": "drafting a plan"},
        {"type": "reasoning", "text": "THE PLAN"},
        {"type": "phase", "label": "Plan reviewer", "detail": "round 1"},
        {"type": "reasoning", "text": "looks good"},
        {"type": "phase", "label": "Executor", "detail": "carrying out the plan"},
        {"type": "reasoning", "text": "THE ANSWER"},
    ]
    # No summary card and no duplicated final-answer message.
    assert not any(i["type"] in ("subagent", "message") for i in items)


def test_a_traced_turn_replays_spawn_cards_but_not_reviewer_cards():
    """A verbose planned turn shows its raw trace instead of verdict cards, but a sub-agent it
    spawned is real work that must still appear."""
    messages = [{"role": "user", "content": "plan something"}]
    items = replay_items(
        messages,
        subagent={
            "0": [
                {"id": "plan-review-0", "role": "reviewer", "status": "done", "issues": ["too vague"]},
                {"id": "r-1", "role": "researcher", "task": "find X", "status": "running"},
                {"id": "r-1", "status": "done", "append": {"kind": "answer", "text": "the answer"}},
            ]
        },
        trace={"0": [{"label": "Planner", "detail": "drafting", "text": "a plan"}]},
    )
    replayed = [item for item in items if item["type"] == "subagent"]
    assert [item["id"] for item in replayed] == ["r-1", "r-1"]


def test_a_traced_turns_spawn_card_keeps_its_closing_status():
    """A spawn whose text streamed closes with a status-only event, carrying neither `task` nor
    `append`. Dropping it leaves the replayed card stuck at "working..." with its answer never
    rendered as markdown, so a lineage's events are kept by id rather than by shape."""
    items = replay_items(
        [{"role": "user", "content": "plan something"}],
        subagent={
            "0": [
                {"id": "plan-review-0", "role": "reviewer", "status": "done", "issues": ["too vague"]},
                {"id": "r-1", "role": "researcher", "task": "find X", "status": "running"},
                {"id": "r-1", "append": {"kind": "answer", "text": "the answer"}},
                {"id": "r-1", "status": "done"},
            ]
        },
        trace={"0": [{"label": "Planner", "detail": "drafting", "text": "a plan"}]},
    )
    replayed = [item for item in items if item["type"] == "subagent"]
    assert [item.get("status") for item in replayed] == ["running", None, "done"]


async def test_web_channel_background_turn_frames_are_muted():
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "hello")

    token = streaming_conversation.set("other")  # a background conversation
    try:
        await channel.send(gen())
        await channel.send_subagent({"id": "r-1", "role": "researcher", "task": "find X", "status": "running"})
    finally:
        streaming_conversation.reset(token)
    assert ws.frames == []  # fully muted, including the "done" terminator and sub-agent cards


async def test_web_channel_foreground_turn_frames_stream():
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "hello")

    token = streaming_conversation.set("viewed")
    try:
        await channel.send(gen())
    finally:
        streaming_conversation.reset(token)
    assert ws.frames == [{"type": "token", "text": "hello"}, {"type": "done"}]


async def test_a_switch_mid_stream_mutes_the_rest_of_the_reply():
    """The muting decision is per frame, not once per send: switching conversations mid-reply must
    not append the rest of that turn's tokens to the conversation now on screen."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "before")
        channel.active_conversation_id = "other"  # the user switches away mid-reply
        yield StreamChunk(StreamingContentType.GENERATING, "after")
        yield StreamChunk(StreamingContentType.THINKING, "thought")

    token = streaming_conversation.set("viewed")
    try:
        await channel.send(gen())
    finally:
        streaming_conversation.reset(token)
    assert ws.frames == [{"type": "token", "text": "before"}]  # no "after", no thinking, no "done"


async def test_a_switch_mid_activity_stream_mutes_the_rest_but_keeps_the_text():
    """Same per-frame rule for the planning path, which still has to return the full text: its
    caller needs the answer regardless of who is watching."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "before")
        channel.active_conversation_id = "other"
        yield StreamChunk(StreamingContentType.GENERATING, "after")

    token = streaming_conversation.set("viewed")
    try:
        text = await channel.stream_activity(gen(), show_answer=True)
    finally:
        streaming_conversation.reset(token)
    assert text == "beforeafter"
    assert ws.frames == [{"type": "token", "text": "before"}]


async def test_switching_back_mid_turn_replays_the_turn_so_far():
    """The whole point of recording a muted turn: a switch-in has to show what the `history` frame it
    rides on cannot, since none of it is in the store until the turn ends."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"
    channel.begin_catch_up("viewed", "ping")

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "hmm")
        yield StreamChunk(StreamingContentType.GENERATING, "seen ")
        channel.active_conversation_id = "other"  # switch away mid-reply
        yield StreamChunk(StreamingContentType.GENERATING, "unseen")

    token = streaming_conversation.set("viewed")
    try:
        await channel.send(gen())
        channel.active_conversation_id = "viewed"  # ...and back, while the turn is still in flight
        await channel.send_history([], {})
    finally:
        streaming_conversation.reset(token)

    items = ws.frames[-1]["items"]
    # The answer replays as a finished `message`: `send` ended with a `done`, which finalizes the bubble
    # here exactly as it does on the page. A turn still mid-answer leaves it an unstamped `partial`.
    assert [item["type"] for item in items] == ["user", "thinking", "message"]
    assert items[0]["text"] == "ping"
    assert items[1]["text"] == "hmm"
    assert items[2]["text"] == "seen unseen"  # both halves: the one the user saw and the muted one


async def test_a_muted_turns_catch_up_keeps_the_tool_output():
    """A background turn's tool frames are recorded verbatim, so the switch-in that replays them shows
    the same output the user would have seen live."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "other"  # the turn below runs out of view
    channel.begin_catch_up("running", "look it up")

    async def gen():
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 2}, "response": "4"})

    token = streaming_conversation.set("running")
    try:
        await channel.send(gen())
        channel.active_conversation_id = "running"
        await channel.send_history([], {})
    finally:
        streaming_conversation.reset(token)

    tool = next(item for item in ws.frames[-1]["items"] if item["type"] == "tool")
    assert tool["response"] == "4"


async def test_the_replayed_answer_keeps_its_place_above_a_later_tool_call():
    """`partial` mirrors the page's own append rule -- a tool call closes the answer segment above it and
    the next tokens open a new bubble below -- so a replay reads in the order a live render produced.

    The closed segment replays as a `message`, not a second `partial`: it is finished prose, so it wants
    the markdown render and the timestamp a finished bubble gets. Only a segment still being written into
    stays a `partial` (see the phase/tool test below, whose turn has no terminator)."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "other"  # background for the whole turn
    channel.begin_catch_up("running", "ping")

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "first ")
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "search", "arguments": {"q": "x"}})
        yield StreamChunk(StreamingContentType.GENERATING, "second")

    token = streaming_conversation.set("running")
    try:
        await channel.send(gen())
        channel.active_conversation_id = "running"
        await channel.send_history([], {})
    finally:
        streaming_conversation.reset(token)

    items = ws.frames[-1]["items"]
    assert [item["type"] for item in items] == ["user", "message", "tool", "message"]
    assert items[1]["text"] == "first "  # written before the call, and still above it
    assert items[-1]["text"] == "second"


async def test_a_persisted_turn_is_not_replayed_twice():
    """`end_catch_up` is what keeps the record from double-rendering: once the turn is in the store, the
    history frame carries it, and the record must no longer add its own copy."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"
    channel.begin_catch_up("viewed", "ping")

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "hello")

    token = streaming_conversation.set("viewed")
    try:
        await channel.send(gen())
    finally:
        streaming_conversation.reset(token)
    channel.end_catch_up("viewed")
    channel.end_catch_up("viewed")  # idempotent: the core calls it on persist and again on turn exit

    await channel.send_history([{"role": "user", "content": "ping"}, {"role": "assistant", "content": "hello"}], {})
    items = ws.frames[-1]["items"]
    assert [item["type"] for item in items] == ["user", "message"]  # from the store alone


async def test_a_conversation_with_no_running_turn_replays_only_the_store():
    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"
    await channel.send_history([{"role": "user", "content": "ping"}], {})
    assert [item["type"] for item in ws.frames[-1]["items"]] == ["user"]


async def test_catch_up_records_the_turns_uploaded_images_and_generated_ones():
    """A user's upload replays under their bubble and a generated image as the assistant's, matching how
    `replay_items` aligns each when the same turn is later replayed from the store."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "other"
    channel.begin_catch_up("running", "look", ["/tmp/kokua-images/abc.png"])

    token = streaming_conversation.set("running")
    try:
        await channel.send_frame({"type": "image", "url": "/images/generated.png"})
    finally:
        streaming_conversation.reset(token)
    channel.active_conversation_id = "running"
    await channel.send_history([], {})

    images = [item for item in ws.frames[-1]["items"] if item["type"] == "image"]
    assert images == [
        {"type": "image", "url": "/images/abc.png", "from": "user", "ts": images[0]["ts"]},
        {"type": "image", "url": "/images/generated.png", "from": "assistant", "ts": images[1]["ts"]},
    ]


async def test_a_background_turns_sidebar_refresh_is_not_muted():
    """`_persist` pushes the conversation list from inside the turn's own task, so a background
    turn's sidebar and history frames carry its conversation in the contextvar. Muting those would
    stop a background turn from ever showing up in the sidebar."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"
    token = streaming_conversation.set("other")
    try:
        await channel.send_conversations([{"key": "other", "title": "Background"}])
        await channel.send_history([], {})
        await channel.send_settings({"planning.plan_review": True})
    finally:
        streaming_conversation.reset(token)
    assert [frame["type"] for frame in ws.frames] == ["conversations", "history", "settings"]


async def test_web_channel_send_notification_always_sends():
    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"
    await channel.send_notification("Task 'Digest' finished")
    assert {"type": "notification", "text": "Task 'Digest' finished"} in ws.frames


async def test_web_channel_send_working_emits_frame_regardless_of_foreground():
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "viewed"
    token = streaming_conversation.set("other")  # a background turn's context; must not mute this
    try:
        await channel.send_working(12.5)
    finally:
        streaming_conversation.reset(token)
    assert ws.frames == [{"type": "working", "active": True, "elapsed": 12.5}]


async def test_web_channel_send_working_with_no_elapsed_reports_idle():
    """None is "no turn running", which is the only thing that turns the page's indicator off. The
    wire keeps a boolean because that is what the page branches on; the Python side takes the
    duration alone so "idle, and it has been running 12s" cannot be expressed."""
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_working(None)
    assert ws.frames == [{"type": "working", "active": False}]


async def test_web_channel_send_approval_request_emits_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_approval_request("add_skill_script", {"skill_name": "x"})
    assert ws.frames == [{"type": "approval", "name": "add_skill_script", "arguments": {"skill_name": "x"}}]


async def test_web_channel_receive_ends_on_sentinel():
    channel = WebChannel(_FakeWS())
    await channel.feed("hello")
    await channel.feed(None)
    msgs = [m async for m in channel.receive()]
    assert len(msgs) == 1
    assert msgs[0].text == "hello" and msgs[0].channel == "web" and msgs[0].sender == "web"


async def test_web_channel_aclose_idempotent():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.aclose()
    await channel.aclose()
    assert ws.closed == 1


# --- History-on-reload -----------------------------------------------------------------------

_CONVERSATION = [
    {"role": "system", "content": "you are an assistant"},
    {"role": "user", "content": "what's 2+2?"},
    {
        "role": "assistant",
        "content": "4",
        "thinking": "adding the numbers",
        "tool_calls": [{"type": "function", "function": {"name": "calc", "arguments": {"x": 2}}, "id": "1"}],
    },
    {"role": "tool", "name": "calc", "content": "4", "tool_call_id": "1"},
]


def test_replay_items_full_replay():
    items = replay_items(_CONVERSATION)
    # message_index is 1, not 0: _CONVERSATION's leading system message shifts the user message
    # one position later, which is exactly the off-by-one the stamping has to get right.
    assert items == [
        {"type": "user", "text": "what's 2+2?", "message_index": 1},
        {"type": "thinking", "text": "adding the numbers"},
        {"type": "message", "text": "4", "proactive": False},
        {"type": "tool", "name": "calc", "arguments": {"x": 2}, "response": "4"},
    ]


def test_replay_items_omits_spawn_subagent_tool_call():
    """Replay must not resurrect the spawn_subagent tool card either -- only its subagent card (fed
    separately via the `subagent` map) represents the spawn."""
    messages = [
        {"role": "user", "content": "do X"},
        {
            "role": "assistant",
            "content": "done",
            "tool_calls": [
                {"type": "function", "function": {"name": SPAWN_SUBAGENT_TOOL_NAME, "arguments": {}}, "id": "1"},
                {"type": "function", "function": {"name": "calc", "arguments": {"x": 2}}, "id": "2"},
            ],
        },
    ]
    items = replay_items(messages)
    tools = [item for item in items if item["type"] == "tool"]
    assert tools == [{"type": "tool", "name": "calc", "arguments": {"x": 2}, "response": None}]


def test_replay_items_attaches_the_tool_result_to_its_call():
    """A stored transcript keeps the call and its result in separate messages, so replay has to rejoin
    them or a reloaded card loses the output a live one showed."""
    items = replay_items(_CONVERSATION)
    tool = next(item for item in items if item["type"] == "tool")
    assert tool["response"] == "4"


def test_replay_items_omits_the_response_when_no_result_message_exists():
    """Conversations stored before results were replayed have the call but no matching result, and a
    turn cut short mid-dispatch never records one. Those cards render as they always did."""
    messages = [
        {"role": "user", "content": "what's 2+2?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"type": "function", "function": {"name": "calc", "arguments": {"x": 2}}, "id": "1"}],
        },
    ]
    items = replay_items(messages)
    assert items[-1] == {"type": "tool", "name": "calc", "arguments": {"x": 2}, "response": None}


def test_replay_items_matches_results_to_calls_by_id_not_position():
    """Concurrent dispatch appends results in completion order, so a positional pairing would hand a
    card the wrong call's output."""
    messages = [
        {"role": "user", "content": "look both up"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "function": {"name": "first", "arguments": {}}, "id": "a"},
                {"type": "function", "function": {"name": "second", "arguments": {}}, "id": "b"},
            ],
        },
        {"role": "tool", "name": "second", "content": "B", "tool_call_id": "b"},
        {"role": "tool", "name": "first", "content": "A", "tool_call_id": "a"},
    ]
    items = replay_items(messages)
    assert [(item["name"], item["response"]) for item in items if item["type"] == "tool"] == [
        ("first", "A"),
        ("second", "B"),
    ]


def test_replay_items_extracts_text_from_content_blocks():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image", "url": "x"}]}]
    # This is about text extraction from a content-block list, not the item's full shape, so it
    # checks the type and the extracted text rather than pinning message_index too.
    items = replay_items(messages)
    assert len(items) == 1
    assert items[0]["type"] == "user" and items[0]["text"] == "hi"


def test_replay_items_empty():
    assert replay_items([]) == []


def test_replay_items_continuation_user_turn_renders_loop_marker_with_prompt_and_reason():
    messages = [{"role": "user", "content": "Continue working.", PROVENANCE_KEY: PROVENANCE_CONTINUATION}]
    assert replay_items(messages) == [{"type": "loop", "reason": "continuation", "text": "Continue working."}]


def test_replay_items_final_answer_user_turn_is_labelled_as_the_cap_not_a_nudge():
    """The two injections say opposite things, so one item shape carrying no reason made a reload
    unable to tell them apart even though the stored message always could."""
    messages = [{"role": "user", "content": "Give the final answer.", PROVENANCE_KEY: PROVENANCE_FINAL_ANSWER}]
    assert replay_items(messages) == [{"type": "loop", "reason": "final_answer", "text": "Give the final answer."}]


def test_replay_items_marks_proactive_assistant_turn():
    messages = [{"role": "assistant", "content": "Don't forget lunch.", PROVENANCE_KEY: PROVENANCE_PROACTIVE}]
    assert replay_items(messages) == [{"type": "message", "text": "Don't forget lunch.", "proactive": True}]


async def test_web_channel_send_history_emits_single_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_history(_CONVERSATION)
    assert len(ws.frames) == 1
    assert ws.frames[0]["type"] == "history"
    # This test is about the whole history landing in one frame, not the user item's exact shape,
    # so it checks for the bubble's type and text rather than a full dict (which now also carries
    # a message_index this test has no stake in).
    assert any(item.get("type") == "user" and item.get("text") == "what's 2+2?" for item in ws.frames[0]["items"])


async def test_web_channel_send_history_empty_sends_empty_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_history([])  # sent even when empty, so switching clears the page
    assert ws.frames == [{"type": "history", "items": []}]


def _drain_until(ws, type_):
    """Receive frames until one of the given type, returning that frame."""
    while True:
        frame = ws.receive_json()
        if frame["type"] == type_:
            return frame


def test_ws_sends_history_on_connect(tmp_path):
    from starlette.testclient import TestClient

    from kokua.core.assistant import Assistant

    cfg = _config(tmp_path)

    async def seed():
        seeder = await Assistant.create(cfg, WebChannel(_FakeWS()), client=MockAsyncModelClient(["Hi!"]))
        await seeder._handle(ChannelMessage(text="hello", channel="web"), conversation_id=seeder._active_id)
        seeder._store.close()  # flush TinyDB so a new connection restores it

    asyncio.run(seed())

    app = build_app(cfg, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _drain_until(ws, "history")  # conversations is sent first, then the restored history

    # Items carry an append-time `ts` (stamped through the model client); assert the rest.
    user_item = next(i for i in frame["items"] if i["type"] == "user")
    message_item = next(i for i in frame["items"] if i["type"] == "message")
    assert user_item["text"] == "hello" and "ts" in user_item
    assert message_item["text"] == "Hi!" and message_item["proactive"] is False and "ts" in message_item


def test_ws_connect_sends_conversations(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        convs = _drain_until(ws, "conversations")
    assert convs["items"]  # at least the fresh active conversation
    assert any(item.get("active") for item in convs["items"])


def test_ws_new_then_select_round_trip(tmp_path):
    import json

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client_factory=lambda cid: MockAsyncModelClient(["reply one"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        # Chat in the first conversation.
        ws.send_text("first message")
        _drain_until(ws, "done")
        # The first message sets the title, which pushes a refreshed list; consume it.
        titled = _drain_until(ws, "conversations")
        assert any(i["title"] == "first message" for i in titled["items"])
        # Start a new conversation; expect a refreshed list with both conversations.
        ws.send_text(json.dumps({"type": "new"}))
        convs = _drain_until(ws, "conversations")
        ids = [i["id"] for i in convs["items"]]
        assert len(ids) == 2
        first_id = next(i["id"] for i in convs["items"] if i["title"] == "first message")
        _drain_until(ws, "history")  # the new conversation's (empty) history
        # Select the first conversation; its history should replay "first message".
        ws.send_text(json.dumps({"type": "select", "id": first_id}))
        hist = _drain_until(ws, "history")
    assert any(item["type"] == "user" and item["text"] == "first message" for item in hist["items"])


def test_ws_delete_active_conversation(tmp_path):
    import json

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client_factory=lambda cid: MockAsyncModelClient(["reply one"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        # Two titled conversations.
        ws.send_text("first message")
        _drain_until(ws, "done")
        _drain_until(ws, "conversations")
        ws.send_text(json.dumps({"type": "new"}))
        _drain_until(ws, "history")
        ws.send_text("second message")
        _drain_until(ws, "done")
        convs = _drain_until(ws, "conversations")
        active_id = next(i["id"] for i in convs["items"] if i["active"])
        # Delete the active conversation; the list drops it and history switches to the remaining one.
        ws.send_text(json.dumps({"type": "delete", "id": active_id}))
        after = _drain_until(ws, "conversations")
        hist = _drain_until(ws, "history")
    ids = [i["id"] for i in after["items"]]
    assert active_id not in ids
    assert len(ids) == 1
    assert any(item["type"] == "user" and item["text"] == "first message" for item in hist["items"])


def test_ws_sends_settings_on_connect(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _drain_until(ws, "settings")
    assert "planning.plan_review" in frame["values"] and "planning.show_reasoning" in frame["values"]


def test_ws_get_and_apply_settings(tmp_path):
    import json

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "settings")  # the connect-time push
        ws.send_text(json.dumps({"type": "get_settings"}))
        _drain_until(ws, "settings")
        # Apply a change to a contributed setting.
        ws.send_text(json.dumps({"type": "settings", "values": {"planning.plan_review": True}}))
        echoed = _drain_until(ws, "settings")
    assert echoed["values"]["planning.plan_review"] is True


def _seed_task(config, name="brief", enabled=True):
    from kokua.config import store

    store.write_task(
        config.config_path,
        name,
        {
            "name": name,
            "prompt": "summarize inbox",
            "schedule": {"type": "interval", "seconds": 3600},
            "max_conversations": 2,
            "created_at": "2026-08-01T00:00:00",
            "enabled": enabled,
        },
    )


def test_ws_sends_tasks_on_connect(tmp_path):
    """The sidebar section is populated without the page having to ask, mirroring the settings push."""
    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _drain_until(ws, "tasks")
    assert [item["name"] for item in frame["items"]] == ["brief"]
    assert frame["items"][0]["name"] == "brief" and frame["items"][0]["enabled"] is True
    # The wire shape the sidebar reads: a task's retention cap, and no dead conversation key.
    assert frame["items"][0]["max_conversations"] == 2 and "session_id" not in frame["items"][0]


def test_ws_get_tasks_returns_the_registry(tmp_path):
    import json

    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "tasks")  # the connect-time push
        ws.send_text(json.dumps({"type": "get_tasks"}))
        frame = _drain_until(ws, "tasks")
    assert [item["name"] for item in frame["items"]] == ["brief"]


def test_ws_get_tasks_reports_a_broken_config_without_dropping_the_connection(tmp_path):
    """list_tasks raises rather than reading an empty list from an unparseable config.toml, so a
    mid-session hand-edit must reach the browser as a message, not close the socket out from under it."""
    import json

    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "tasks")  # the connect-time push, while config.toml still parses
        # An unclosed table header is the same kind of slip a hand-edit could leave mid-session.
        config.config_path.write_text("[scheduling.task.x\nprompt = 'p'\n", encoding="utf-8")
        ws.send_text(json.dumps({"type": "get_tasks"}))
        message = _drain_until(ws, "message")
        assert "could not" in message["text"].lower()
        # The connection survives: an unrelated control still gets its normal answer afterward.
        ws.send_text(json.dumps({"type": "get_settings"}))
        _drain_until(ws, "settings")


def test_ws_task_disable_applies_and_echoes_fresh_tasks(tmp_path):
    import json

    from kokua.config import store
    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "tasks")
        ws.send_text(json.dumps({"type": "task", "action": "disable", "name": "brief"}))
        echoed = _drain_until(ws, "tasks")
    assert echoed["items"][0]["enabled"] is False
    assert store.load_tasks(config.config_path)[0]["enabled"] is False


def test_ws_task_delete_removes_the_record(tmp_path):
    import json

    from kokua.config import store
    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "tasks")
        ws.send_text(json.dumps({"type": "task", "action": "delete", "name": "brief"}))
        echoed = _drain_until(ws, "tasks")
    assert echoed["items"] == []
    assert store.load_tasks(config.config_path) == []


def test_ws_task_rejects_unknown_action_without_touching_the_registry(tmp_path):
    import json

    from kokua.config import store
    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "tasks")
        ws.send_text(json.dumps({"type": "task", "action": "drop_table", "name": "brief"}))
        message = _drain_until(ws, "message")
    assert "could not" in message["text"].lower()
    assert store.load_tasks(config.config_path)[0].get("enabled", True) is True


def test_a_task_control_frame_addresses_the_task_by_name(tmp_path):
    import json

    from kokua.config import store
    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config, name="hourly")
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "tasks")
        ws.send_text(json.dumps({"type": "task", "action": "disable", "name": "hourly"}))
        _drain_until(ws, "tasks")
    assert store.load_tasks(config.config_path)[0]["enabled"] is False


async def test_the_task_frame_carries_names_not_ids(tmp_path):
    from kokua.core.assistant import Assistant

    config = _config(tmp_path)
    _seed_task(config, name="hourly")
    assistant = await Assistant.create(config, WebChannel(_FakeWS()), client=MockAsyncModelClient([]))
    items = assistant.list_tasks()
    assert items[0]["name"] == "hourly"
    assert "id" not in items[0]


def test_ws_conversations_carry_their_task_id(tmp_path):
    """The page nests a task's conversations under it, so the sidebar payload has to say which task
    minted each conversation."""
    from starlette.testclient import TestClient

    config = _config(tmp_path)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _drain_until(ws, "conversations")
    assert all("task_id" in item for item in frame["items"])


def test_ws_reports_model_client_error_and_releases_busy(tmp_path, monkeypatch):
    import kokua.core.assistant as assistant_mod
    from starlette.testclient import TestClient

    def boom(*args, **kwargs):
        raise ValueError("No model specified and no default could be resolved.")

    # client=None makes each connection build its own via Assistant.create, so it hits aio.client.
    monkeypatch.setattr(assistant_mod.aio, "client", boom)
    app = build_app(_config(tmp_path))

    def first_message(ws):
        while True:
            frame = ws.receive_json()
            if frame["type"] == "message":
                return frame["text"]

    with TestClient(app).websocket_connect("/ws") as ws:
        text = first_message(ws)
    assert "no default could be resolved" in text

    # The busy guard was released on the failed build, so a second connection is not refused; it
    # reaches the build path again rather than being told the assistant is busy in another tab.
    with TestClient(app).websocket_connect("/ws") as ws:
        text = first_message(ws)
    assert "busy in another tab" not in text
    assert "no default could be resolved" in text


def test_ws_reports_config_error_and_releases_busy(tmp_path, monkeypatch):
    """A config.toml that stopped resolving (a toolset renamed out from under an [agents.*] table)
    reaches the browser as its own message.

    Only ModelClientError used to be caught here, so a ConfigError escaped into Starlette: the socket
    closed with no frame, leaving the page able to say nothing but "Disconnected.", and the busy guard
    it left set made every reload afterwards claim the assistant was busy in another tab.
    """
    import kokua.frontends.web as web_mod
    from starlette.testclient import TestClient

    from kokua.config import ConfigError

    async def boom(*args, **kwargs):
        raise ConfigError("agent 'report-writer' declares unknown toolset 'pdf'.")

    monkeypatch.setattr(web_mod.Assistant, "create", boom)
    app = build_app(_config(tmp_path))

    def first_message(ws):
        while True:
            frame = ws.receive_json()
            if frame["type"] == "message":
                return frame["text"]

    with TestClient(app).websocket_connect("/ws") as ws:
        text = first_message(ws)
    assert "unknown toolset 'pdf'" in text

    with TestClient(app).websocket_connect("/ws") as ws:
        text = first_message(ws)
    assert "busy in another tab" not in text
    assert "unknown toolset 'pdf'" in text


def test_ws_releases_busy_when_the_build_fails_unexpectedly(tmp_path, monkeypatch):
    """Any failure before the serve loop releases the guard, not just the two it can name.

    The guard is set before the assistant is built but was released only by the serve loop's own
    `finally`, so an exception in between leaked it and every later connection was refused as busy --
    a wrong diagnosis of a fault that had nothing to do with another tab.
    """
    import kokua.frontends.web as web_mod
    from starlette.testclient import TestClient

    async def boom(*args, **kwargs):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(web_mod.Assistant, "create", boom)
    app = build_app(_config(tmp_path))
    client = TestClient(app)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            with client.websocket_connect("/ws"):
                pass


def test_build_app_rejects_agents_that_cannot_resolve(tmp_path):
    """The web front end builds its assistant per connection, so a broken [agents.*] table would
    otherwise be reported only by a WebSocket that closes: `kokua --frontend web` would sit there
    serving a page and refusing every connection it made. Validating the agents while building the app
    puts the message on the terminal at startup, which is where the CLI front end already reports it.
    """
    from kokua.config import ConfigError

    agents = example_agents()
    agents["assistant"].tools = ["memory", "pdf"]
    with pytest.raises(ConfigError, match="pdf"):
        build_app(_config(tmp_path, agents=agents))


def test_ws_new_conversation_reports_model_client_error_without_dropping_connection(tmp_path):
    import json

    from starlette.testclient import TestClient

    from kokua.core.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial conversation builds fine; a later one fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    app = build_app(_config(tmp_path), client_factory=factory)

    def first_message(ws):
        while True:
            frame = ws.receive_json()
            if frame["type"] == "message":
                return frame["text"]

    # Before the fix, this raised ModelClientError out of pump(), which tore down the websocket via
    # the enclosing TaskGroup; the exception surfaced when the `with` block below was exited.
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        ws.send_text(json.dumps({"type": "new"}))
        text = first_message(ws)
    assert "could not be created" in text


def test_ws_select_conversation_reports_model_client_error_without_dropping_connection(tmp_path):
    import json

    from starlette.testclient import TestClient

    from kokua.core.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial conversation builds fine; the second (selected) one fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    app = build_app(_config(tmp_path), client_factory=factory)

    def first_message(ws):
        while True:
            frame = ws.receive_json()
            if frame["type"] == "message":
                return frame["text"]

    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        ws.send_text(json.dumps({"type": "select", "id": "does-not-exist-yet"}))
        text = first_message(ws)
    assert "could not be opened" in text


def test_ws_delete_conversation_reports_model_client_error_without_dropping_connection(tmp_path):
    import json

    from starlette.testclient import TestClient

    from kokua.core.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        # The initial conversation builds fine; deleting it forces a fresh replacement conversation,
        # whose build fails.
        if calls["n"] > 1:
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    app = build_app(_config(tmp_path), client_factory=factory)

    def first_message(ws):
        while True:
            frame = ws.receive_json()
            if frame["type"] == "message":
                return frame["text"]

    with TestClient(app).websocket_connect("/ws") as ws:
        convs = _drain_until(ws, "conversations")
        active_id = next(i["id"] for i in convs["items"] if i["active"])
        ws.send_text(json.dumps({"type": "delete", "id": active_id}))
        text = first_message(ws)
    assert "could not be deleted" in text


def test_download_route_serves_documents(tmp_path):
    from starlette.testclient import TestClient

    cfg = _config(tmp_path)
    cfg.downloads_path.mkdir(parents=True, exist_ok=True)
    (cfg.downloads_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    client = TestClient(build_app(cfg, client=MockAsyncModelClient([])))

    resp = client.get("/download/report.pdf")
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 fake"
    assert "application/pdf" in resp.headers["content-type"]

    assert client.get("/download/missing.pdf").status_code == 404  # no such file
    # A nested path can't match the single-segment {name} route, so nothing outside the folder is reachable.
    assert client.get("/download/sub/evil.pdf").status_code == 404


def test_ws_plan_autonomous_emits_plan_then_answer(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient(["THE PLAN", "THE ANSWER"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("/plan do the thing")
        plan = _drain_until(ws, "plan")
        frames = []
        while True:
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "done":
                break
    assert plan["text"] == "THE PLAN"
    assert {"type": "token", "text": "THE ANSWER"} in frames


def test_ws_plan_review_approve_then_executes(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(
        _config(tmp_path, toolset_settings=planning_settings(plan_review=True)),
        client=MockAsyncModelClient(["THE PLAN", "THE ANSWER"]),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("/plan do X")
        assert _drain_until(ws, "plan")["text"] == "THE PLAN"
        assert _drain_until(ws, "plan_review")["plan"] == "THE PLAN"  # paused for review
        ws.send_text("approve")
        frames = []
        while True:
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "done":
                break
    assert {"type": "token", "text": "THE ANSWER"} in frames


def test_ws_plan_review_reject_skips_execution(tmp_path):
    from starlette.testclient import TestClient

    # Only the plan response is queued; if execution ran it would raise (index error), so a clean
    # "(plan rejected)" message proves execution was skipped.
    app = build_app(
        _config(tmp_path, toolset_settings=planning_settings(plan_review=True)),
        client=MockAsyncModelClient(["THE PLAN"]),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("/plan do X")
        _drain_until(ws, "plan_review")
        ws.send_text("reject")
        msg = _drain_until(ws, "message")
    assert "rejected" in msg["text"]


def test_ws_plan_review_agent_surfaces_critique_to_human(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from kokua.workflows.critics import Verdict

    async def reject(*a, **k):
        return Verdict(approved=False, issues=["needs a verification step"])

    monkeypatch.setattr("kokua.workflows.planning.critics.review_plan", reject)
    app = build_app(
        _config(
            tmp_path, toolset_settings=planning_settings(plan_review=True, plan_review_agent=True, review_rounds=0)
        ),
        client=MockAsyncModelClient(["THE PLAN"]),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("/plan do X")
        frame = _drain_until(ws, "plan_review")
        ws.send_text("reject")
        _drain_until(ws, "message")
    assert frame["critique"] and "verification" in frame["critique"]


def test_ws_subagent_frames_live_and_replayed(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from kokua.workflows.critics import Verdict

    async def reject(*a, **k):
        return Verdict(approved=False, issues=["needs a verification step"])

    monkeypatch.setattr("kokua.workflows.planning.critics.review_plan", reject)
    # review_rounds=0 -> one plan review (rejected), then proceed autonomously and execute.
    app = build_app(
        _config(tmp_path, toolset_settings=planning_settings(plan_review_agent=True, review_rounds=0)),
        client=MockAsyncModelClient(["THE PLAN", "THE ANSWER"]),
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("/plan do X")
        running = _drain_until(ws, "subagent")
        assert running["status"] == "running" and running["role"] == "Plan reviewer"
        verdict = _drain_until(ws, "subagent")
        assert verdict["status"] == "rejected" and "verification" in verdict["issues"][0]
        _drain_until(ws, "done")

    # A fresh connection replays the recorded reviewer card from history.
    with TestClient(app).websocket_connect("/ws") as ws:
        hist = _drain_until(ws, "history")
    subs = [i for i in hist["items"] if i["type"] == "subagent"]
    assert subs and subs[0]["status"] == "rejected"


def test_ws_slash_plan_triggers_planning(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient(["THE PLAN", "THE ANSWER"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("/plan do X")
        assert _drain_until(ws, "plan")["text"] == "THE PLAN"  # the per-request /plan drafts a plan


# --- Concurrent conversations: active-conversation sync + working indicator ------------------


async def test_assistant_active_id_and_turn_running_accessors(tmp_path):
    from kokua.core.assistant import Assistant

    assistant = await Assistant.create(_config(tmp_path), WebChannel(_FakeWS()), client=MockAsyncModelClient([]))
    assert assistant.active_id == assistant._active_id
    assert assistant.turn_running(assistant.active_id) is False
    assert assistant.turn_running("no-such-conversation") is False


async def test_select_sets_active_conversation_and_streams_from_now(tmp_path):
    # After selecting a conversation, the channel's active_conversation_id matches, so its turn streams
    # (Assistant._sync_channel_active_id, exercised here directly rather than through the pump).
    from kokua.core.assistant import Assistant

    channel = WebChannel(_FakeWS())
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient([]))
    first_id = assistant.active_id
    await assistant.new_conversation()
    assert channel.active_conversation_id == assistant.active_id != first_id

    await assistant.select_conversation(first_id)
    assert channel.active_conversation_id == assistant.active_id == first_id


async def test_sync_view_sets_active_conversation_id_and_refreshes_state_without_running_turn(tmp_path):
    from kokua.core.assistant import Assistant
    from kokua.frontends.web import _sync_view

    ws = _FakeWS()
    channel = WebChannel(ws)
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))

    await _sync_view(channel, assistant)

    assert channel.active_conversation_id == assistant.active_id
    types = [f["type"] for f in ws.frames]
    assert "conversations" in types and "history" in types
    assert "working" not in types  # nothing is running, so no working indicator


async def test_switch_into_running_conversation_sends_working_indicator(tmp_path):
    # Selecting (or connecting into) a conversation that has an in-flight turn emits a "working" frame,
    # so the page shows the turn is still going rather than looking idle.
    from aimu.aio import RunHandle
    from kokua.core.assistant import Assistant
    from kokua.frontends.web import _sync_view
    from kokua.core.turn_registry import TurnInfo

    ws = _FakeWS()
    channel = WebChannel(ws)
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    started = time.monotonic() - 30  # a turn already half a minute old when the view switches into it
    assistant._tracker.add(assistant.active_id, TurnInfo(handle=handle, started=started, preview="p"))
    try:
        await _sync_view(channel, assistant)
    finally:
        handle.cancel()
        await asyncio.gather(handle.task, return_exceptions=True)

    working = [f for f in ws.frames if f.get("type") == "working"]
    assert [f["active"] for f in working] == [True]
    # The page counts up from this rather than from the moment of the switch, so a turn already
    # running for a while does not read as one that just started.
    assert working[0]["elapsed"] >= 30


# --- Server round-trip via Starlette TestClient ----------------------------------------------


def test_ws_round_trip(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient(["Hello there."]))
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("hi")
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "done":
                break
    assert {"type": "token", "text": "Hello there."} in frames
    assert frames[-1] == {"type": "done"}


def test_ws_input_frame_thinking_reaches_the_stored_turn(tmp_path):
    """The one seam no unit test reaches on its own: `_parse_input` and `feed_input` are each tested in
    isolation, and the core seam is tested against a hand-built `ChannelMessage`, but nothing joins
    them end to end. A mis-wire in `frontends/web.py`'s `pump` (the line that calls
    `feed_input(text, paths, thinking=thinking)`) would pass every one of those tests and still lose
    the effort on the wire, so this drives a real `{"type": "input", ...}` frame over the socket and
    checks what actually landed in the conversation's stored provenance."""
    import json

    from aimu.sessions import TinyDBSessionStore
    from starlette.testclient import TestClient

    cfg = _config(tmp_path)
    app = build_app(cfg, client=MockAsyncModelClient(["Hello there."]))
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "input", "text": "hi", "thinking": "high"}))
        while True:
            frame = ws.receive_json()
            if frame["type"] == "done":
                break

    store = TinyDBSessionStore(str(cfg.sessions_path))
    (key,) = store.list_keys()
    session = store.get(key)
    assert "high" in session.metadata["thinking"].values()


def test_index_route_serves_html(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient([]))
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


def test_vendored_js_served(tmp_path):
    from starlette.testclient import TestClient

    client = TestClient(build_app(_config(tmp_path), client=MockAsyncModelClient([])))
    for name, marker in [("marked.min.js", "marked"), ("purify.min.js", "DOMPurify")]:
        resp = client.get("/" + name)
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert marker in resp.text  # the library's own name appears in its source/header
    assert client.get("/nope.js").status_code == 404


def test_vendored_katex_js_css_and_fonts_served(tmp_path):
    import re

    from starlette.testclient import TestClient

    client = TestClient(build_app(_config(tmp_path), client=MockAsyncModelClient([])))

    js = client.get("/katex.min.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/auto-render.min.js").status_code == 200

    css = client.get("/katex.min.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")

    # Every woff2 the CSS references must resolve from the /fonts/ route as a real woff2 file.
    fonts = sorted(set(re.findall(r"fonts/(KaTeX_[A-Za-z0-9-]+\.woff2)", css.text)))
    assert fonts, "expected the KaTeX CSS to reference woff2 fonts"
    for name in fonts:
        resp = client.get(f"/fonts/{name}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "font/woff2"
        assert resp.content[:4] == b"wOF2"  # woff2 magic bytes

    # The allowlist rejects anything that is not a vendored KaTeX font.
    assert client.get("/fonts/evil.woff2").status_code == 404
    assert client.get("/fonts/KaTeX_Main-Regular.ttf").status_code == 404


def test_page_css_and_js_served(tmp_path):
    """The page's own stylesheet and script are separate files, so they must serve like the vendored
    ones do. The index references them by relative URL; a 404 here is a page with no styling."""
    from starlette.testclient import TestClient

    client = TestClient(build_app(_config(tmp_path), client=MockAsyncModelClient([])))

    css = client.get("/app.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "--fg" in css.text  # the token layer the whole sheet is built on

    js = client.get("/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "WebSocket" in js.text

    index = client.get("/").text
    assert 'href="app.css"' in index
    assert 'src="app.js"' in index


async def test_a_second_answer_after_a_phase_replays_as_its_own_bubble():
    """A verbose planned turn closes the answer at each phase, so a second one is a second bubble --
    including when both hold the same text, where floating the open one by equality would move the
    wrong item."""
    from kokua.channels.web import streaming_conversation

    ws = _FakeWS()
    channel = WebChannel(ws)
    channel.active_conversation_id = "other"
    channel.begin_catch_up("running", "plan it")

    token = streaming_conversation.set("running")
    try:
        await channel.send_frame({"type": "token", "text": "same"})
        await channel.send_frame({"type": "phase", "label": "Reviewer", "detail": ""})
        await channel.send_frame({"type": "token", "text": "same"})
        await channel.send_frame({"type": "tool", "name": "search", "arguments": {}})
        await channel.send_frame({"type": "token", "text": " more"})
    finally:
        streaming_conversation.reset(token)
    channel.active_conversation_id = "running"
    await channel.send_history([], {})

    items = ws.frames[-1]["items"]
    assert [item["type"] for item in items] == ["user", "message", "phase", "message", "tool", "partial"]
    assert items[1]["text"] == "same"  # the first answer stayed where the phase closed it
    assert items[3]["text"] == "same"  # the second stayed where the tool call closed it
    assert items[5]["text"] == " more"


def test_replay_items_replays_an_answer_above_the_calls_it_preceded():
    """A stored assistant message holds its prose and the calls it went on to make, and the prose came
    first: the model wrote it, then called the tools. Replay has to keep that order, or a reload
    rearranges a turn the user watched arrive the other way round."""
    messages = [
        {"role": "user", "content": "change both tasks"},
        {
            "role": "assistant",
            "content": "I see both tasks.",
            "thinking": "which two?",
            "tool_calls": [{"id": "1", "function": {"name": "update_scheduled_task", "arguments": {"name": "news"}}}],
        },
        {"role": "tool", "tool_call_id": "1", "content": "updated"},
        {"role": "assistant", "content": "Both are changed."},
    ]

    items = replay_items(messages)

    assert [item["type"] for item in items] == ["user", "thinking", "message", "tool", "message"]
    assert items[2]["text"] == "I see both tasks."


def test_replay_items_closes_a_failed_turn_with_its_reason():
    """A failed turn ends mid-exchange, so without the notice the replay just stops with no account of
    why -- the ``_report`` line that explained it went to whichever conversation the user was viewing.
    Placed after the turn's own output, where a reader looking for the end of the turn will find it."""
    messages = [{"role": "user", "content": "scan them"}, {"role": "assistant", "content": "starting"}]
    items = replay_items(messages, failure={"0": "failed: out of context"})
    assert items[-1] == {"type": "notice", "text": "failed: out of context"}


def test_replay_items_keeps_a_failed_turns_reason_inside_that_turn():
    """The user can carry on in a conversation whose earlier turn failed, so the notice belongs at the
    end of the turn it describes rather than at the end of the transcript."""
    messages = [
        {"role": "user", "content": "scan them"},
        {"role": "assistant", "content": "starting"},
        {"role": "user", "content": "try again"},
        {"role": "assistant", "content": "done"},
    ]
    items = replay_items(messages, failure={"0": "failed: out of context"})
    kinds = [(i["type"], i.get("text")) for i in items]
    assert kinds == [
        ("user", "scan them"),
        ("message", "starting"),
        ("notice", "failed: out of context"),
        ("user", "try again"),
        ("message", "done"),
    ]


def test_replay_items_inherits_the_turn_timestamp_for_a_failure_notice():
    messages = [
        {"role": "user", "content": "scan them", "timestamp": "2026-08-19T06:43:56"},
        {"role": "assistant", "content": "starting", "timestamp": "2026-08-19T06:44:10"},
    ]
    items = replay_items(messages, failure={"0": "failed: boom"})
    assert next(i for i in items if i["type"] == "notice")["ts"] == "2026-08-19T06:43:56"


def test_replay_items_omits_the_notice_by_default():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    items = replay_items(messages)
    assert not any(i["type"] == "notice" for i in items)


def test_ws_task_stop_also_refreshes_the_conversation_list(tmp_path):
    """The panel decides whether to offer Stop from the running marker on a task's conversations, so a
    stop has to refresh that list too -- unlike the other task actions, which only touch the registry."""
    import json

    from starlette.testclient import TestClient

    config = _config(tmp_path)
    _seed_task(config)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "tasks")
        ws.send_text(json.dumps({"type": "task", "action": "stop", "name": "brief"}))
        _drain_until(ws, "conversations")
        echoed = _drain_until(ws, "tasks")
    assert [item["name"] for item in echoed["items"]] == ["brief"]  # a stop leaves the task in place


def test_ws_conversations_carry_whether_a_turn_is_running(tmp_path):
    """The flag the page reads for its spinner and for the task panel's Stop button."""
    from starlette.testclient import TestClient

    config = _config(tmp_path)
    app = build_app(config, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _drain_until(ws, "conversations")
    assert frame["items"] and frame["items"][0]["running"] is False


# --- the input frame: images, a per-turn effort, or both -----------------------------------------


def test_parse_input_reads_an_effort_off_a_frame_with_no_images():
    """The frame is no longer image-only. A text message with a chosen effort has exactly this shape,
    and rejecting it here would drop its text back to the plain-string path, feeding the model the raw
    JSON as if the user had typed it."""
    from kokua.frontends.web import _parse_input

    parsed = _parse_input('{"type": "input", "text": "hello", "thinking": "high"}')

    assert parsed == ("hello", [], "high")


def test_parse_input_reads_images_and_an_effort_together():
    from kokua.frontends.web import _parse_input

    parsed = _parse_input('{"type": "input", "text": "what is this?", "images": ["data:x"], "thinking": "off"}')

    assert parsed == ("what is this?", ["data:x"], "off")


def test_parse_input_returns_no_effort_when_the_frame_carries_none():
    from kokua.frontends.web import _parse_input

    assert _parse_input('{"type": "input", "text": "hi", "images": ["data:x"]}') == ("hi", ["data:x"], None)


def test_parse_input_ignores_a_non_string_effort():
    """The page cannot send this, but the socket is not the page. The core would drop it anyway; dropping
    it here keeps a malformed frame from reaching the core at all."""
    from kokua.frontends.web import _parse_input

    assert _parse_input('{"type": "input", "text": "hi", "thinking": 3}') == ("hi", [], None)


def test_parse_input_declines_anything_that_is_not_an_input_frame():
    from kokua.frontends.web import _parse_input

    assert _parse_input("just a message") is None
    assert _parse_input('{"type": "select", "id": "abc"}') is None


def test_web_channel_feed_input_puts_the_effort_on_the_message_metadata():
    async def run():
        channel = WebChannel(_FakeWS())
        await channel.feed_input("think hard", [], thinking="high")
        await channel.feed(None)
        return [m async for m in channel.receive()]

    received = asyncio.run(run())
    assert len(received) == 1
    assert received[0].text == "think hard"
    assert received[0].images is None
    assert received[0].metadata["thinking"] == "high"


def test_web_channel_feed_input_leaves_metadata_empty_without_an_effort():
    """Absence has to stay absent: the core reads a missing key as "use the configured effort"."""

    async def run():
        channel = WebChannel(_FakeWS())
        await channel.feed_input("what is this?", ["/tmp/a.png"])
        await channel.feed(None)
        return [m async for m in channel.receive()]

    received = asyncio.run(run())
    assert received[0].images == ["/tmp/a.png"]
    assert "thinking" not in received[0].metadata


class _AsgiSocket:
    """Drives the ``/ws`` endpoint the way a real server does: queued inbound messages, captured sends.

    ``TestClient`` cannot stand in here. It delivers a disconnect by cancelling the task the app runs in,
    which papers over exactly the failure below: uvicorn cancels nothing, it hands the app a
    ``websocket.disconnect`` message and expects the app to notice. An app that has stopped reading never
    does.
    """

    def __init__(self, inbound: list[dict]):
        self._inbound: asyncio.Queue = asyncio.Queue()
        for message in inbound:
            self._inbound.put_nowait(message)
        self.sent: list[dict] = []

    async def receive(self) -> dict:
        return await self._inbound.get()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    def feed(self, message: dict) -> None:
        self._inbound.put_nowait(message)

    def frames(self) -> list[dict]:
        import json

        return [json.loads(m["text"]) for m in self.sent if m.get("type") == "websocket.send" and "text" in m]


def _ws_scope() -> dict:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "scheme": "ws",
        "path": "/ws",
        "raw_path": b"/ws",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 8000),
        "subprotocols": [],
        "state": {},
    }


async def test_a_blocked_control_still_releases_the_connection_on_disconnect(tmp_path):
    """A control that blocks must not cost the user their way back into the UI.

    The socket reader used to handle every control inline, so a control waiting on the turn gate (a
    settings apply drains every in-flight turn) stopped the reader entirely. The disconnect that followed
    was never read, the one-connection guard was never released, and reloading the page was refused as
    "busy in another tab": the only way out was killing the process.
    """
    import json

    blocking = BlockingModelClient()
    app = build_app(_config(tmp_path), client=blocking)

    first = _AsgiSocket([{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "a long job"}])
    serving = asyncio.create_task(app(_ws_scope(), first.receive, first.send))
    try:
        await asyncio.wait_for(blocking.started.wait(), timeout=5)  # the turn now holds the gate
        first.feed({"type": "websocket.receive", "text": json.dumps({"type": "settings", "values": {}})})
        first.feed({"type": "websocket.disconnect", "code": 1000})

        done, _ = await asyncio.wait({serving}, timeout=5)  # wait(), not wait_for(): must not cancel it
        assert serving in done, "the connection never noticed the disconnect while a control was blocked"

        second = _AsgiSocket([{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}])
        await asyncio.wait_for(app(_ws_scope(), second.receive, second.send), timeout=5)
        refusals = [f for f in second.frames() if "busy in another tab" in str(f.get("text", ""))]
        assert not refusals, "a reload after the disconnect was refused as busy"
    finally:
        blocking.release.set()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def test_a_control_that_raises_tears_the_connection_down(tmp_path):
    """The other direction of the same rule: the reader must not outlive whatever applies frames.

    Applying frames in its own task is what stops a slow control wedging the socket, but it also means an
    unexpected error there can kill that task alone. A reader left running past it would go on queueing
    frames into something nobody drains, which is the wedge again with the halves swapped.
    """
    import json

    calls: list[str] = []

    def factory(conversation_id):
        calls.append(conversation_id)
        if len(calls) > 1:
            raise RuntimeError("agent build exploded")
        return MockAsyncModelClient(["ok"])

    app = build_app(_config(tmp_path), client_factory=factory)
    sock = _AsgiSocket(
        [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": json.dumps({"type": "new"})}]
    )
    serving = asyncio.create_task(app(_ws_scope(), sock.receive, sock.send))

    done, _ = await asyncio.wait({serving}, timeout=5)
    assert serving in done, "a control that raised left the socket being read with nothing applying frames"
    assert serving.exception() is not None, "the error was swallowed instead of ending the connection"


def test_the_export_control_writes_a_file_and_sends_a_download_frame(tmp_path):
    """The page needs a name to fetch, and /download/{name} already serves downloads_path."""
    import json

    from starlette.testclient import TestClient

    config = _config(tmp_path)
    app = build_app(config, client_factory=lambda cid: MockAsyncModelClient(["the reply"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        ws.send_text("a question")
        _drain_until(ws, "done")
        convs = _drain_until(ws, "conversations")
        active_id = next(i["id"] for i in convs["items"] if i["active"])
        ws.send_text(json.dumps({"type": "export", "id": active_id}))
        frame = _drain_until(ws, "download")
    assert frame["url"] == f"/download/{frame['name']}"
    written = (config.downloads_path / frame["name"]).read_text(encoding="utf-8")
    assert written.startswith("# ")
    assert "a question" in written


def test_the_exported_name_is_a_bare_filename_the_download_route_will_serve(tmp_path):
    """The route rejects anything where name != Path(name).name, so a title with a slash in it
    would otherwise produce a file the page is told to fetch and cannot."""
    import json
    from pathlib import Path

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client_factory=lambda cid: MockAsyncModelClient(["ok"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        ws.send_text("re: a/b/c and ../.. slashes")
        _drain_until(ws, "done")
        convs = _drain_until(ws, "conversations")
        active_id = next(i["id"] for i in convs["items"] if i["active"])
        ws.send_text(json.dumps({"type": "export", "id": active_id}))
        frame = _drain_until(ws, "download")
    assert frame["name"] == Path(frame["name"]).name


def test_the_exported_file_is_actually_fetchable_over_the_download_route(tmp_path):
    """The two halves have to agree: the frame's url and the route's guard."""
    import json

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client_factory=lambda cid: MockAsyncModelClient(["ok"]))
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        ws.send_text("a question")
        _drain_until(ws, "done")
        convs = _drain_until(ws, "conversations")
        active_id = next(i["id"] for i in convs["items"] if i["active"])
        ws.send_text(json.dumps({"type": "export", "id": active_id}))
        frame = _drain_until(ws, "download")
    response = client.get(frame["url"])
    assert response.status_code == 200
    assert response.text.startswith("# ")


def test_exporting_an_unknown_conversation_answers_rather_than_closing_the_socket(tmp_path):
    import json

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client_factory=lambda cid: MockAsyncModelClient(["ok"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        ws.send_text(json.dumps({"type": "export", "id": "no-such-conversation"}))
        frame = _drain_until(ws, "message")
        # The socket is still live: another control still answers.
        ws.send_text(json.dumps({"type": "get_tasks"}))
        _drain_until(ws, "tasks")
    assert "export" in frame["text"].lower()


def test_the_export_control_does_not_refresh_the_sidebar_or_history(tmp_path):
    """An export reads a conversation; it must not also push a fresh conversation list or a history
    replay, since neither the active conversation nor what the page is displaying has changed.

    A trailing "get_tasks" control (which always answers with exactly one "tasks" frame) is what makes
    this a real check rather than a tautology: the socket is served by one queue and one applying task,
    so "get_tasks" cannot be answered until everything the "export" control queued has already been
    sent, and collecting every frame up to "tasks" (instead of draining straight to "download" with
    _drain_until, which would silently step over an unwanted "conversations"/"history" frame on the
    way) is what lets the two frames in between be asserted as none at all.
    """
    import json

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client_factory=lambda cid: MockAsyncModelClient(["ok"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "conversations")
        ws.send_text("a question")
        _drain_until(ws, "done")
        convs = _drain_until(ws, "conversations")
        active_id = next(i["id"] for i in convs["items"] if i["active"])
        _drain_until(ws, "turn_saved")  # the turn just answered publishes its own start position
        ws.send_text(json.dumps({"type": "export", "id": active_id}))
        ws.send_text(json.dumps({"type": "get_tasks"}))
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "tasks":
                break
    assert [f["type"] for f in frames] == ["download", "tasks"]
