"""Mock-only tests for image input/output: on-disk storage, session compaction, transport, display."""

from __future__ import annotations

import asyncio
import io
import json

from helpers import MockAsyncModelClient
from kokua import images
from kokua.assistant import _compact_message_images, _expand_message_images
from kokua.channels.cli import CLIChannel
from kokua.channels.web import WebChannel, conversation_to_frames
from kokua.config import AssistantConfig
from kokua.frontends.web import build_app

# A 1x1 PNG, the smallest valid image; used to exercise the real encode/decode paths.
_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _config(tmp_path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False}
    base.update(overrides)
    return AssistantConfig(**base)


# --- images helper module --------------------------------------------------------------------


def test_save_data_url_writes_content_addressed_file(tmp_path):
    ref = images.save_data_url(tmp_path, _PNG_DATA_URL)
    assert ref.startswith("/images/")
    assert ref.endswith(".png")
    path = images.reference_to_path(tmp_path, ref)
    assert path is not None and path.is_file()
    # Same bytes -> same reference (idempotent, deduped).
    assert images.save_data_url(tmp_path, _PNG_DATA_URL) == ref


def test_save_data_url_rejects_non_data_url(tmp_path):
    assert images.save_data_url(tmp_path, "https://example.com/cat.png") is None


def test_reference_to_data_url_round_trips(tmp_path):
    ref = images.save_data_url(tmp_path, _PNG_DATA_URL)
    assert images.reference_to_data_url(tmp_path, ref) == _PNG_DATA_URL


def test_reference_to_path_blocks_traversal_and_missing(tmp_path):
    assert images.reference_to_path(tmp_path, "/images/../secret") is None
    assert images.reference_to_path(tmp_path, "/images/nope.png") is None
    assert images.reference_to_path(tmp_path, "not-a-reference") is None


# --- session compaction / expansion ----------------------------------------------------------


def _image_message(url: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": url}}],
    }


def test_compact_then_expand_round_trip(tmp_path):
    compacted = _compact_message_images([_image_message(_PNG_DATA_URL)], tmp_path)
    stored_url = compacted[0]["content"][1]["image_url"]["url"]
    assert stored_url.startswith("/images/")  # small reference, not base64
    assert "base64" not in json.dumps(compacted)

    expanded = _expand_message_images(compacted, tmp_path)
    assert expanded[0]["content"][1]["image_url"]["url"] == _PNG_DATA_URL


def test_compact_leaves_non_data_urls_and_text_only(tmp_path):
    http_msg = _image_message("https://example.com/x.png")
    text_msg = {"role": "user", "content": "plain text"}
    out = _compact_message_images([http_msg, text_msg], tmp_path)
    assert out[0]["content"][1]["image_url"]["url"] == "https://example.com/x.png"
    assert out[1] is text_msg  # untouched messages are shared, not copied


def test_expand_missing_file_left_as_reference(tmp_path):
    msg = _image_message("/images/deadbeef.png")
    out = _expand_message_images([msg], tmp_path)
    assert out[0]["content"][1]["image_url"]["url"] == "/images/deadbeef.png"


# --- conversation_to_frames display -----------------------------------------------------------


def test_conversation_to_frames_emits_user_image_item():
    messages = [_image_message("/images/abc.png")]
    items = conversation_to_frames(messages, show_thinking=True, show_tools=True)
    assert {"type": "user", "text": "look"} in items
    assert {"type": "image", "url": "/images/abc.png", "from": "user"} in items


def test_conversation_to_frames_emits_generated_image_from_tool_result():
    messages = [
        {"role": "tool", "name": "generate_image", "content": "Generated image (/images/gen.png).", "tool_call_id": "1"}
    ]
    items = conversation_to_frames(messages, show_thinking=False, show_tools=False)
    assert items == [{"type": "image", "url": "/images/gen.png", "from": "assistant"}]


# --- web transport: input frame -> ChannelMessage.images --------------------------------------


class _FakeWS:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self):
        pass


def test_web_channel_feed_input_populates_images():
    async def run():
        channel = WebChannel(_FakeWS())
        await channel.feed_input("what is this?", ["/tmp/a.png"])
        await channel.feed(None)
        received = [m async for m in channel.receive()]
        return received

    received = asyncio.run(run())
    assert len(received) == 1
    assert received[0].text == "what is this?"
    assert received[0].images == ["/tmp/a.png"]


def test_ws_image_input_flows_to_model_and_persists_reference(tmp_path):
    from starlette.testclient import TestClient

    cfg = _config(tmp_path)
    client = MockAsyncModelClient(["I see a tiny dot."])
    app = build_app(cfg, client=client)
    with TestClient(app).websocket_connect("/ws") as ws:
        _drain_until(ws, "history")
        ws.send_text(json.dumps({"type": "input", "text": "describe", "images": [_PNG_DATA_URL]}))
        _drain_until(ws, "done")  # a reactive reply streams tokens then a terminal 'done'; persist runs after

    # The upload was saved on disk and the model saw an image content block.
    assert list(cfg.images_path.glob("*.png"))
    user_msg = next(m for m in client.messages if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(b.get("type") == "image_url" for b in user_msg["content"])

    # The persisted session stores a small /images reference, not inline base64.
    raw = cfg.sessions_path.read_text()
    assert "/images/" in raw
    assert "base64" not in raw


def _drain_until(ws, frame_type):
    seen = []
    for _ in range(200):
        frame = ws.receive_json()
        seen.append(frame["type"])
        if frame["type"] == frame_type:
            return frame
    raise AssertionError(f"frame {frame_type!r} not received; saw {seen}")


# --- /images serving route --------------------------------------------------------------------


def test_images_route_serves_and_guards(tmp_path):
    from starlette.testclient import TestClient

    cfg = _config(tmp_path)
    cfg.images_path.mkdir(parents=True, exist_ok=True)
    (cfg.images_path / "pic.png").write_bytes(b"\x89PNG fake")
    client = TestClient(build_app(cfg, client=MockAsyncModelClient([])))

    resp = client.get("/images/pic.png")
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG fake"
    assert client.get("/images/missing.png").status_code == 404
    assert client.get("/images/sub/evil.png").status_code == 404  # traversal blocked by the route converter


# --- image generation toolpack ----------------------------------------------------------------


def test_image_toolpack_gated_on_model_env(tmp_path, monkeypatch):
    from kokua.toolpacks.image import build

    monkeypatch.delenv("AIMU_IMAGE_MODEL", raising=False)
    assert build(_config(tmp_path)) == []  # no model configured -> no tool offered

    monkeypatch.setenv("AIMU_IMAGE_MODEL", "gemini:nano-banana")
    tools = build(_config(tmp_path))
    assert [fn.__name__ for fn in tools] == ["generate_image"]


# --- CLI /attach -----------------------------------------------------------------------------


def test_cli_attach_stages_image_onto_next_message(tmp_path, monkeypatch):
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr("sys.stdin", io.StringIO(f"/attach {img}\nwhat is this?\n"))

    async def run():
        return [m async for m in CLIChannel().receive()]

    messages = asyncio.run(run())
    assert len(messages) == 1
    assert messages[0].text == "what is this?"
    assert messages[0].images == [str(img)]
