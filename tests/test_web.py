"""Mock-only tests for the web front end (WebChannel + Starlette server)."""

from __future__ import annotations

import asyncio

from helpers import MockAsyncModelClient
from kokua.channels.web import WebChannel, conversation_to_frames
from kokua.config import AssistantConfig
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


async def test_web_channel_send_emits_loop_marker_on_iteration_increment():
    from aimu.aio.agent import DEFAULT_CONTINUATION_PROMPT

    ws = _FakeWS()
    channel = WebChannel(ws)

    async def gen():
        yield StreamChunk(StreamingContentType.GENERATING, "a", iteration=0)
        yield StreamChunk(StreamingContentType.GENERATING, "b", iteration=1)

    await channel.send(gen())
    assert ws.frames == [
        {"type": "token", "text": "a"},
        {"type": "loop", "text": DEFAULT_CONTINUATION_PROMPT},
        {"type": "token", "text": "b"},
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


async def test_web_channel_send_settings_emits_frame():
    ws = _FakeWS()
    channel = WebChannel(ws)
    values = {"model": "m1", "show_thinking": True, "show_tools": False, "generate_kwargs": {"temperature": 0.3}}
    await channel.send_settings(values)
    assert ws.frames == [{"type": "settings", "values": values}]


async def test_web_channel_stream_activity_shows_loop_withholds_answer():
    ws = _FakeWS()
    channel = WebChannel(ws, show_thinking=True, show_tools=True)

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "pondering", iteration=0)
        yield StreamChunk(StreamingContentType.TOOL_CALLING, {"name": "calc", "arguments": {"x": 1}}, iteration=0)
        yield StreamChunk(StreamingContentType.GENERATING, "the ", iteration=1)  # withheld; iteration bump -> loop
        yield StreamChunk(StreamingContentType.GENERATING, "answer", iteration=1)

    answer = await channel.stream_activity(gen())
    assert answer == "the answer"  # accumulated, not streamed
    types = [f["type"] for f in ws.frames]
    assert "thinking" in types and "tool" in types and "loop" in types  # the loop stays visible
    assert "token" not in types and "done" not in types  # answer withheld, no terminator


async def test_web_channel_stream_activity_show_answer_emits_tokens():
    ws = _FakeWS()
    channel = WebChannel(ws, show_thinking=True, show_tools=True)

    async def gen():
        yield StreamChunk(StreamingContentType.THINKING, "hmm")
        yield StreamChunk(StreamingContentType.GENERATING, "the ")
        yield StreamChunk(StreamingContentType.GENERATING, "answer")

    text = await channel.stream_activity(gen(), show_answer=True)
    assert text == "the answer"  # still captured
    types = [f["type"] for f in ws.frames]
    assert types.count("token") == 2 and "thinking" in types  # answer shown live (verbose)
    assert "done" not in types  # no terminator; caller ends the turn


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


def test_conversation_to_frames_interleaves_subagent_after_user():
    messages = [{"role": "user", "content": "do X"}, {"role": "assistant", "content": "done"}]
    subagent = {"0": [{"role": "Plan reviewer", "status": "rejected", "issues": ["x"], "round": 0}]}
    items = conversation_to_frames(messages, show_thinking=True, show_tools=True, subagent=subagent)
    assert items[0] == {"type": "user", "text": "do X"}
    assert items[1]["type"] == "subagent" and items[1]["status"] == "rejected"
    assert items[2]["type"] == "message"


def test_conversation_to_frames_omits_subagent_by_default():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    items = conversation_to_frames(messages, show_thinking=True, show_tools=True)
    assert not any(i["type"] == "subagent" for i in items)


def test_conversation_to_frames_replays_verbose_trace_not_committed_answer():
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
    items = conversation_to_frames(messages, show_thinking=True, show_tools=True, trace=trace)
    assert items == [
        {"type": "user", "text": "do X"},
        {"type": "phase", "label": "Planner", "detail": "drafting a plan"},
        {"type": "reasoning", "text": "THE PLAN"},
        {"type": "phase", "label": "Plan reviewer", "detail": "round 1"},
        {"type": "reasoning", "text": "looks good"},
        {"type": "phase", "label": "Executor", "detail": "carrying out the plan"},
        {"type": "reasoning", "text": "THE ANSWER"},
    ]
    # No summary card and no duplicated final-answer message.
    assert not any(i["type"] in ("subagent", "message") for i in items)


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


def test_conversation_to_frames_continuation_user_turn_renders_loop_marker_with_prompt():
    messages = [{"role": "user", "content": "Continue working.", PROVENANCE_KEY: PROVENANCE_CONTINUATION}]
    assert conversation_to_frames(messages, show_thinking=False, show_tools=False) == [
        {"type": "loop", "text": "Continue working."}
    ]


def test_conversation_to_frames_final_answer_user_turn_renders_loop_marker_with_prompt():
    messages = [{"role": "user", "content": "Give the final answer.", PROVENANCE_KEY: PROVENANCE_FINAL_ANSWER}]
    assert conversation_to_frames(messages, show_thinking=False, show_tools=False) == [
        {"type": "loop", "text": "Give the final answer."}
    ]


def test_conversation_to_frames_marks_proactive_assistant_turn():
    messages = [{"role": "assistant", "content": "Don't forget lunch.", PROVENANCE_KEY: PROVENANCE_PROACTIVE}]
    assert conversation_to_frames(messages, show_thinking=False, show_tools=False) == [
        {"type": "message", "text": "Don't forget lunch.", "proactive": True}
    ]


async def test_web_channel_send_history_emits_single_frame():
    ws = _FakeWS()
    channel = WebChannel(ws, show_thinking=True, show_tools=True)
    await channel.send_history(_CONVERSATION)
    assert len(ws.frames) == 1
    assert ws.frames[0]["type"] == "history"
    assert {"type": "user", "text": "what's 2+2?"} in ws.frames[0]["items"]


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

    from kokua.assistant import Assistant

    cfg = _config(tmp_path)

    async def seed():
        seeder = await Assistant.create(cfg, WebChannel(_FakeWS()), client=MockAsyncModelClient(["Hi!"]))
        await seeder._handle(ChannelMessage(text="hello", channel="web"))
        seeder._store.close()  # flush TinyDB so a new connection restores it

    asyncio.run(seed())

    app = build_app(cfg, client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _drain_until(ws, "history")  # conversations is sent first, then the restored history

    assert {"type": "user", "text": "hello"} in frame["items"]
    assert {"type": "message", "text": "Hi!", "proactive": False} in frame["items"]


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

    app = build_app(_config(tmp_path), client=MockAsyncModelClient(["reply one"]))
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

    app = build_app(_config(tmp_path), client=MockAsyncModelClient(["reply one", "reply two"]))
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
    assert "generate_kwargs" in frame["values"]
    assert "show_thinking" in frame["values"] and "show_tools" in frame["values"]


def test_ws_get_and_apply_settings(tmp_path):
    import json

    from starlette.testclient import TestClient

    app = build_app(_config(tmp_path), client=MockAsyncModelClient([]))
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "settings")  # the connect-time push
        ws.send_text(json.dumps({"type": "get_settings"}))
        _drain_until(ws, "settings")
        # Apply a kwargs + display change (no model switch, so no real client is built).
        ws.send_text(
            json.dumps({"type": "settings", "values": {"generate_kwargs": {"temperature": 0.6}, "show_tools": False}})
        )
        echoed = _drain_until(ws, "settings")
    assert echoed["values"]["generate_kwargs"]["temperature"] == 0.6
    assert echoed["values"]["show_tools"] is False


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

    app = build_app(_config(tmp_path, plan_review=True), client=MockAsyncModelClient(["THE PLAN", "THE ANSWER"]))
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
    app = build_app(_config(tmp_path, plan_review=True), client=MockAsyncModelClient(["THE PLAN"]))
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("/plan do X")
        _drain_until(ws, "plan_review")
        ws.send_text("reject")
        msg = _drain_until(ws, "message")
    assert "rejected" in msg["text"]


def test_ws_plan_review_agent_surfaces_critique_to_human(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from kokua.review import Verdict

    async def reject(*a, **k):
        return Verdict(approved=False, issues=["needs a verification step"])

    monkeypatch.setattr("kokua.review.review_plan", reject)
    app = build_app(
        _config(tmp_path, plan_review=True, plan_review_agent=True, review_rounds=0),
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

    from kokua.review import Verdict

    async def reject(*a, **k):
        return Verdict(approved=False, issues=["needs a verification step"])

    monkeypatch.setattr("kokua.review.review_plan", reject)
    # review_rounds=0 -> one plan review (rejected), then proceed autonomously and execute.
    app = build_app(
        _config(tmp_path, plan_review_agent=True, review_rounds=0),
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
