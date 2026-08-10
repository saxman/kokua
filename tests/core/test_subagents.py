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


async def test_a_spawn_opens_a_running_card_and_closes_it_with_the_answer():
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


async def test_generated_text_is_not_streamed_into_the_card():
    """The answer lands once, on completion, so the card never shows a half-written answer twice."""
    reporter, channel = _reporter(show_thinking=True, show_tools=True)
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", StreamChunk(StreamingContentType.GENERATING, "partial"))
    assert len(channel.subagent_frames) == 1


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
    await reporter.finished("r-1", "done", None)
    assert events == [
        {"id": "r-1", "role": "researcher", "task": "find X", "status": "running"},
        {"id": "r-1", "append": {"kind": "reasoning", "text": "one two"}},
        {"id": "r-1", "status": "done", "append": {"kind": "answer", "text": "done"}},
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
