"""The sub-agent reporter: AIMU spawn callbacks become display frames plus recorded events."""

from __future__ import annotations

import asyncio

from aimu.models import StreamChunk, StreamingContentType

from kokua.channels.ui import ChannelUI
from kokua.core.subagents import SubagentReporter, subagent_events
from tests.channels import SubagentCapturingChannel


def _reporter(**flags):
    channel = SubagentCapturingChannel(**flags)
    return SubagentReporter(ChannelUI(channel)), channel


def _collect():
    """Install a per-turn collector and return the list the reporter appends to."""
    events: list[dict] = []
    subagent_events.set(events)
    return events


def _thinking(text):
    return StreamChunk(StreamingContentType.THINKING, text)


def _tool_call(name, arguments):
    return StreamChunk(StreamingContentType.TOOL_CALLING, {"name": name, "arguments": arguments})


def _generating(text):
    return StreamChunk(StreamingContentType.GENERATING, text)


async def test_a_spawn_opens_a_running_card_and_closes_it_with_the_answer():
    """A provider that yields no GENERATING chunk streams nothing, so the terminal event carries the
    text rather than leaving the card empty."""
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("researcher-abc", "researcher", "find X")
    await reporter.finished("researcher-abc", "the answer", None)
    assert channel.subagent_frames == [
        {"id": "researcher-abc", "role": "researcher", "task": "find X", "status": "running"},
        {"id": "researcher-abc", "status": "done", "append": {"kind": "answer", "text": "the answer"}},
    ]


async def test_generic_spawn_without_a_role_still_names_the_card():
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("subagent-abc", None, "find X")
    assert channel.subagent_frames[0]["role"] == "subagent"


async def test_nested_reasoning_needs_show_thinking():
    reporter, channel = _reporter(show_thinking=False)
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking("hmm"))
    assert len(channel.subagent_frames) == 1

    reporter, channel = _reporter(show_thinking=True)
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking("hmm"))
    assert channel.subagent_frames[-1] == {"id": "r-1", "append": {"kind": "reasoning", "text": "hmm"}}


async def test_nested_tool_calls_need_show_tools():
    reporter, channel = _reporter(show_tools=False)
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _tool_call("get_webpage", {"url": "https://example.com"}))
    assert len(channel.subagent_frames) == 1

    reporter, channel = _reporter(show_tools=True)
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _tool_call("get_webpage", {"url": "https://example.com"}))
    assert channel.subagent_frames[-1] == {
        "id": "r-1",
        "append": {"kind": "tool", "name": "get_webpage", "arguments": {"url": "https://example.com"}},
    }


async def test_generated_text_streams_chunk_by_chunk_and_is_not_gated():
    """The card's text arrives live like the parent's own answer, which the display flags don't gate
    either."""
    reporter, channel = _reporter(show_thinking=False, show_tools=False)
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("half "))
    await reporter.chunk("r-1", _generating("an answer"))
    assert channel.subagent_frames[1:] == [
        {"id": "r-1", "append": {"kind": "answer", "text": "half "}},
        {"id": "r-1", "append": {"kind": "answer", "text": "an answer"}},
    ]


async def test_a_streamed_answer_is_not_repeated_by_the_finish_frame():
    """The text is already on screen, so repeating it on completion would show the answer twice."""
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("the answer"))
    await reporter.finished("r-1", "the answer", None)
    assert channel.subagent_frames[-1] == {"id": "r-1", "status": "done"}
    assert events[-1] == {"id": "r-1", "status": "done"}


async def test_answer_chunks_coalesce_when_recorded_but_arrive_as_separate_frames():
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("one "))
    await reporter.chunk("r-1", _generating("two"))
    await reporter.finished("r-1", "one two", None)
    assert [f.get("append", {}).get("text") for f in channel.subagent_frames[1:]] == ["one ", "two", None]
    assert events == [
        {"id": "r-1", "role": "researcher", "task": "find X", "status": "running"},
        {"id": "r-1", "append": {"kind": "answer", "text": "one two"}},
        {"id": "r-1", "status": "done"},
    ]


async def test_a_tool_call_between_two_generations_starts_a_second_answer_entry():
    """One answer entry per round, so a multi-round spawn reads as rounds rather than one run-on
    block. The parent's own iterations are separated the same way, by its continuation marker."""
    reporter, _channel = _reporter(show_tools=True)
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("first round"))
    await reporter.chunk("r-1", _tool_call("get_webpage", {"url": "u"}))
    await reporter.chunk("r-1", _generating("second round"))
    assert [e.get("append", {}).get("text") for e in events[1:]] == ["first round", None, "second round"]


async def test_an_interleaved_spawn_breaks_the_answer_block():
    reporter, _channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "a")
    await reporter.spawned("r-2", "researcher", "b")
    await reporter.chunk("r-1", _generating("one"))
    await reporter.chunk("r-2", _generating("two"))
    await reporter.chunk("r-1", _generating("three"))
    assert [(e["id"], e["append"]["text"]) for e in events if "append" in e] == [
        ("r-1", "one"),
        ("r-2", "two"),
        ("r-1", "three"),
    ]


async def test_the_streamed_answer_marker_is_released_when_the_spawn_finishes():
    """The reporter lives as long as the connection, so tracking which spawns streamed must not grow
    an entry per spawn ever made."""
    reporter, _channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("the answer"))
    await reporter.finished("r-1", "the answer", None)
    assert reporter._streamed_answers == set()


async def test_a_stopped_spawn_keeps_what_it_streamed():
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "writer", "draft it")
    await reporter.chunk("r-1", _generating("partial dra"))
    await reporter.finished("r-1", "partial dra", asyncio.CancelledError())
    assert channel.subagent_frames[1:] == [
        {"id": "r-1", "append": {"kind": "answer", "text": "partial dra"}},
        {"id": "r-1", "status": "stopped"},
    ]


async def test_a_failed_spawn_ends_the_card_in_error():
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.finished("r-1", "", ValueError("child exploded"))
    assert channel.subagent_frames[-1] == {
        "id": "r-1",
        "status": "error",
        "append": {"kind": "error", "text": "child exploded"},
    }
    assert events[-1]["status"] == "error"


async def test_a_cancelled_spawn_records_before_it_tries_to_send():
    """The reporter runs inside the cancelled task, so recording must not depend on the send."""

    class _RefusingChannel(SubagentCapturingChannel):
        async def send_subagent(self, event):
            raise asyncio.CancelledError

    reporter = SubagentReporter(ChannelUI(_RefusingChannel()))
    events = _collect()
    await reporter.finished("r-1", "partial", asyncio.CancelledError())
    assert events == [{"id": "r-1", "status": "stopped", "append": {"kind": "answer", "text": "partial"}}]


async def test_events_are_recorded_for_replay_and_reasoning_is_coalesced():
    reporter, _channel = _reporter(show_thinking=True)
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking("one "))
    await reporter.chunk("r-1", _thinking("two"))
    await reporter.finished("r-1", "the answer", None)
    assert events == [
        {"id": "r-1", "role": "researcher", "task": "find X", "status": "running"},
        {"id": "r-1", "append": {"kind": "reasoning", "text": "one two"}},
        {"id": "r-1", "status": "done", "append": {"kind": "answer", "text": "the answer"}},
    ]


async def test_coalescing_never_merges_across_two_concurrent_spawns():
    reporter, _channel = _reporter(show_thinking=True)
    events = _collect()
    await reporter.spawned("r-1", "researcher", "a")
    await reporter.spawned("r-2", "researcher", "b")
    await reporter.chunk("r-1", _thinking("one"))
    await reporter.chunk("r-2", _thinking("two"))
    await reporter.chunk("r-1", _thinking("three"))
    reasoning = [e["append"]["text"] for e in events if "append" in e]
    assert reasoning == ["one", "two", "three"]


async def test_coalescing_survives_interleaving_with_another_turns_activity():
    """One reporter serves every conversation's turns, and those run concurrently by default. A
    reporter-level 'last reasoning' slot (the old design) would let one turn's events clobber another
    turn's coalescing state; this drives two turns as real overlapping tasks, each with its own
    subagent_events list, and asserts each still coalesces its own consecutive chunks into one entry,
    in order, despite the interleaving."""
    reporter, _channel = _reporter(show_thinking=True)

    async def run_turn(spawn_id, chunks):
        events: list[dict] = []
        subagent_events.set(events)
        await reporter.spawned(spawn_id, "researcher", "find X")
        for text in chunks:
            await reporter.chunk(spawn_id, _thinking(text))
            await asyncio.sleep(0)  # yield, so the two turns' record() calls genuinely interleave
        await reporter.finished(spawn_id, "done", None)
        return events

    events_a, events_b = await asyncio.gather(
        run_turn("r-1", ["one ", "two ", "three"]),
        run_turn("r-2", ["uno ", "dos"]),
    )

    def reasoning_of(events):
        return [e["append"]["text"] for e in events if e.get("append", {}).get("kind") == "reasoning"]

    assert reasoning_of(events_a) == ["one two three"]
    assert reasoning_of(events_b) == ["uno dos"]


async def test_recording_is_a_copy_so_coalescing_cannot_mutate_a_sent_frame():
    reporter, channel = _reporter(show_thinking=True)
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking("one "))
    await reporter.chunk("r-1", _thinking("two"))
    assert [f["append"]["text"] for f in channel.subagent_frames[1:]] == ["one ", "two"]


async def test_no_collector_installed_still_displays():
    """A spawn outside any turn (there is no such path today) must not raise."""
    reporter, channel = _reporter()
    subagent_events.set(None)
    await reporter.spawned("r-1", "researcher", "find X")
    assert len(channel.subagent_frames) == 1
