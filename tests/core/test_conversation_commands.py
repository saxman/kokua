"""The conversation commands: /new, /conversations, and /switch."""

from __future__ import annotations

import asyncio

import pytest

from aimu.aio.channels.base import ChannelMessage
from aimu.sessions import Session

from kokua.core.assistant import Assistant
from kokua.core.build import ModelClientError
from tests.channels import FakeChannel, _ConvCapturingChannel, _config
from tests.helpers import BlockingModelClient, MockAsyncModelClient


def _transcript(channel: FakeChannel) -> str:
    """Everything the channel was told to show, as one blob to assert against."""
    return "\n".join(channel.sent)


async def _serve(assistant: Assistant) -> None:
    await assistant._serve_channel()


async def test_new_starts_and_switches_to_an_empty_conversation(tmp_path):
    channel = FakeChannel(["/new"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    first = assistant._active_id

    await _serve(assistant)

    assert assistant._active_id != first
    assert assistant._session.messages == []
    assert assistant._active_id in assistant._store.list_keys()


async def test_new_does_not_send_the_command_to_the_model(tmp_path):
    """The gap this feature closes: /new used to be an ordinary chat message."""
    channel = FakeChannel(["/new"])
    client = MockAsyncModelClient(["I cannot start a new conversation."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await _serve(assistant)

    assert assistant._tracker.get(assistant._active_id) is None
    assert "I cannot start a new conversation." not in _transcript(channel)


async def test_conversations_lists_every_conversation_and_marks_the_active_one(tmp_path):
    channel = FakeChannel(["/new", "/conversations"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))

    await _serve(assistant)

    listing = channel.sent[-1]
    for key in assistant._store.list_keys():
        assert key[:6] in listing
    active_line = next(line for line in listing.splitlines() if line.startswith("*"))
    assert assistant._active_id[:6] in active_line


async def test_switch_moves_the_active_pointer_by_id_fragment(tmp_path):
    channel = FakeChannel([])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    first = assistant._active_id
    await assistant.new_conversation()
    assert assistant._active_id != first

    channel._inbound = [f"/switch {first[:6]}"]
    await _serve(assistant)

    assert assistant._active_id == first


async def test_switch_without_an_argument_reports_how_to_use_it(tmp_path):
    channel = FakeChannel(["/switch"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    before = assistant._active_id

    await _serve(assistant)

    assert assistant._active_id == before
    assert "/switch" in _transcript(channel)


async def test_switch_to_an_unknown_fragment_says_so_and_stays_put(tmp_path):
    channel = FakeChannel(["/switch ffffffff"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    before = assistant._active_id

    await _serve(assistant)

    assert assistant._active_id == before
    assert "ffffffff" in _transcript(channel)
    assert "/conversations" in _transcript(channel)


async def test_switch_on_a_fragment_shorter_than_the_minimum_says_how_much_is_needed(tmp_path):
    channel = FakeChannel(["/switch ab"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))

    await _serve(assistant)

    assert "6" in _transcript(channel)


async def test_switch_on_an_ambiguous_fragment_prints_the_full_ids(tmp_path):
    """A fragment that names two conversations is a dead end unless the reply carries enough to retype."""
    channel = FakeChannel([])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    twins = ["abcdef" + "1" * 26, "abcdef" + "2" * 26]
    for key in twins:
        assistant._store.save(Session(key=key, metadata={"created_at": "2026-01-01", "updated_at": "2026-01-01"}))

    channel._inbound = ["/switch abcdef"]
    await _serve(assistant)

    assert all(key in _transcript(channel) for key in twins)


async def test_switching_away_leaves_a_running_turn_running(tmp_path):
    """Architecture rule: switching backgrounds a turn, it does not cancel it."""
    started = asyncio.Event()

    class _Channel(FakeChannel):
        async def receive(self):
            yield ChannelMessage(text="long task", channel="fake")
            await started.wait()
            yield ChannelMessage(text="/new", channel="fake")

    client = BlockingModelClient("finished")
    channel = _Channel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    first = assistant._active_id

    async def watch():
        await client.started.wait()
        started.set()

    try:
        await asyncio.gather(_serve(assistant), watch())
        assert assistant._active_id != first
        info = assistant._tracker.get(first)
        assert info is not None and not info.handle.done
        assert "still running" in _transcript(channel)
    finally:
        client.release.set()
        info = assistant._tracker.get(first)
        if info is not None:
            await asyncio.gather(info.handle.task, return_exceptions=True)

    assert any(m.get("content") == "long task" for m in assistant._store.get(first).messages)


async def test_a_switch_repaints_a_channel_that_draws_a_whole_conversation(tmp_path):
    class _ViewChannel(_ConvCapturingChannel):
        def __init__(self):
            super().__init__()
            self.histories: list[list[dict]] = []
            self.active_conversation_id = None

        async def send_history(self, messages, metadata=None):
            self.histories.append(messages)

    channel = _ViewChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    channel._inbound = ["/new"]

    await _serve(assistant)

    assert channel.histories == [[]]  # the new, empty conversation
    marked_active = [item["id"] for item in channel.conversation_pushes[-1] if item["active"]]
    assert marked_active == [assistant._active_id]


async def test_a_failure_to_build_the_new_agent_is_reported_not_raised(tmp_path):
    """The serve loop must survive it: an exception here would close the channel for good."""
    channel = FakeChannel(["/new"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    before = assistant._active_id

    def refuse(conversation_id: str):
        raise ModelClientError("no model")

    assistant._registry._build = refuse

    await _serve(assistant)

    assert assistant._active_id == before
    assert "could not" in _transcript(channel).lower()


async def test_a_message_that_merely_starts_with_a_slash_still_runs_a_turn(tmp_path):
    channel = FakeChannel(["/usr/local/bin is missing"])
    client = MockAsyncModelClient(["Check your PATH."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await _serve(assistant)
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:
        await info.handle.task

    assert "Check your PATH." in _transcript(channel)


@pytest.mark.parametrize("command", ["/new", "/conversations", "/switch abcdef"])
async def test_the_commands_never_reach_the_model(tmp_path, command):
    channel = FakeChannel([command])
    client = MockAsyncModelClient(["a reply nobody asked for"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await _serve(assistant)

    assert "a reply nobody asked for" not in _transcript(channel)
