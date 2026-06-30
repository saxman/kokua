"""Mock-only tests for the web front end (WebChannel + Starlette server)."""

from __future__ import annotations

import asyncio

from helpers import MockAsyncModelClient
from kokua.channels.web import WebChannel, conversation_to_frames
from kokua.config import AssistantConfig
from kokua.frontends.web import build_app

from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamChunk, StreamingContentType


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
    base = {"data_dir": tmp_path, "memory": False}
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


async def test_web_channel_emits_thinking_and_tool_frames_when_enabled():
    ws = _FakeWS()
    channel = WebChannel(ws, show_thinking=True, show_tools=True)

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "hmm")
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 2}})
        yield StreamChunk(StreamingContentType.GENERATING, "4")

    await channel.send(gen())
    assert ws.frames == [
        {"type": "thinking", "text": "hmm"},
        {"type": "tool", "name": "calc", "arguments": {"x": 2}},
        {"type": "token", "text": "4"},
        {"type": "done"},
    ]


async def test_web_channel_skips_empty_generating_chunks():
    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "")
        yield StreamChunk(StreamingContentType.GENERATING, "hi")
        yield StreamChunk(StreamingContentType.GENERATING, "")

    await channel.send(gen())
    assert ws.frames == [{"type": "token", "text": "hi"}, {"type": "done"}]


async def test_web_channel_omits_thinking_and_tool_frames_by_default():
    ws = _FakeWS()
    channel = WebChannel(ws)  # defaults: show_thinking / show_tools off

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "hmm")
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {}})
        yield StreamChunk(StreamingContentType.GENERATING, "4")

    await channel.send(gen())
    assert ws.frames == [{"type": "token", "text": "4"}, {"type": "done"}]


async def test_web_channel_send_conversations_emits_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    items = [{"id": "a1", "title": "Trip plan", "active": True}]
    await channel.send_conversations(items)
    assert ws.frames == [{"type": "conversations", "items": items}]


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


def test_conversation_to_frames_full_replay():
    items = conversation_to_frames(_CONVERSATION, show_thinking=True, show_tools=True)
    assert items == [
        {"type": "user", "text": "what's 2+2?"},
        {"type": "thinking", "text": "adding the numbers"},
        {"type": "tool", "name": "calc", "arguments": {"x": 2}},
        {"type": "message", "text": "4", "proactive": False},
    ]


def test_conversation_to_frames_gating():
    items = conversation_to_frames(_CONVERSATION, show_thinking=False, show_tools=False)
    assert items == [
        {"type": "user", "text": "what's 2+2?"},
        {"type": "message", "text": "4", "proactive": False},
    ]


def test_conversation_to_frames_extracts_text_from_content_blocks():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image", "url": "x"}]}]
    assert conversation_to_frames(messages, show_thinking=True, show_tools=True) == [{"type": "user", "text": "hi"}]


def test_conversation_to_frames_empty():
    assert conversation_to_frames([], show_thinking=True, show_tools=True) == []


async def test_web_channel_send_history_emits_single_frame():
    ws = _FakeWS()
    channel = WebChannel(ws, show_thinking=True, show_tools=True)
    await channel.send_history(_CONVERSATION)
    assert len(ws.frames) == 1
    assert ws.frames[0]["type"] == "history"
    assert {"type": "user", "text": "what's 2+2?"} in ws.frames[0]["items"]


async def test_web_channel_send_history_empty_sends_nothing():
    ws = _FakeWS()
    channel = WebChannel(ws)
    await channel.send_history([])
    assert ws.frames == []


def test_ws_sends_history_on_connect(tmp_path):
    from starlette.testclient import TestClient

    from kokua.assistant import Assistant

    cfg = _config(tmp_path)

    async def seed():
        seeder = await Assistant.create(cfg, WebChannel(_FakeWS()), client=MockAsyncModelClient(["Hi!"]))
        await seeder._handle(ChannelMessage(text="hello", channel="web"))
        seeder._store.close()  # flush TinyDB so a new connection restores it

    asyncio.run(seed())

    app = build_app(cfg, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = ws.receive_json()  # first frame on connect should be the restored history

    assert frame["type"] == "history"
    assert {"type": "user", "text": "hello"} in frame["items"]
    assert {"type": "message", "text": "Hi!", "proactive": False} in frame["items"]


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


def test_index_route_serves_html(tmp_path):
    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient([]))
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()
