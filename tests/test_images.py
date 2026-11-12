"""Mock-only tests for image input/output: on-disk storage, session compaction, transport, display."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json

from tests.helpers import MockAsyncModelClient
from kokua import images
from kokua.core.messages import compact_message_images, expand_message_images
from kokua.core.transcripts import replay_items
from kokua.channels.cli import CLIChannel
from kokua.channels.web import WebChannel
from kokua.config import AssistantConfig
from tests.channels import example_agents
from kokua.frontends.web import build_app

# A 1x1 PNG, the smallest valid image; used to exercise the real encode/decode paths.
_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _config(tmp_path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "agents": example_agents(), "entry_agent": "assistant"}
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
    compacted = compact_message_images([_image_message(_PNG_DATA_URL)], tmp_path)
    stored_url = compacted[0]["content"][1]["image_url"]["url"]
    assert stored_url.startswith("/images/")  # small reference, not base64
    assert "base64" not in json.dumps(compacted)

    expanded = expand_message_images(compacted, tmp_path)
    assert expanded[0]["content"][1]["image_url"]["url"] == _PNG_DATA_URL


def test_compact_leaves_non_data_urls_and_text_only(tmp_path):
    http_msg = _image_message("https://example.com/x.png")
    text_msg = {"role": "user", "content": "plain text"}
    out = compact_message_images([http_msg, text_msg], tmp_path)
    assert out[0]["content"][1]["image_url"]["url"] == "https://example.com/x.png"
    assert out[1] is text_msg  # untouched messages are shared, not copied


def _generated_image_turn(url: str) -> list[dict]:
    """A generate_image call and the result AIMU stores for it on a vision model: text plus the image."""
    call = {"type": "function", "function": {"name": "generate_image", "arguments": {"prompt": "a giraffe"}}}
    return [
        {"role": "user", "content": "draw a giraffe"},
        {"role": "assistant", "content": "", "tool_calls": [{**call, "id": "call-0"}]},
        {
            "role": "tool",
            "name": "generate_image",
            "tool_call_id": "call-0",
            "content": [
                {"type": "text", "text": "Generated image, shown to the user (/images/x.png)."},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        },
    ]


def test_a_generated_image_in_a_tool_result_is_stored_as_a_reference(tmp_path):
    """The image the model was sent is stored by reference like a user's, and re-inlined on restore, so
    the model can still see it after a restart without sessions.json carrying its bytes."""
    compacted = compact_message_images(_generated_image_turn(_PNG_DATA_URL), tmp_path)
    assert compacted[2]["content"][1]["image_url"]["url"].startswith("/images/")
    assert "base64" not in json.dumps(compacted)
    assert expand_message_images(compacted, tmp_path)[2]["content"][1]["image_url"]["url"] == _PNG_DATA_URL


def test_a_replayed_tool_card_shows_the_result_text_not_the_image_blocks():
    (card,) = [item for item in replay_items(_generated_image_turn("/images/abc.png")) if item["type"] == "tool"]
    assert card["response"] == "Generated image, shown to the user (/images/x.png)."


def test_expand_missing_file_left_as_reference(tmp_path):
    msg = _image_message("/images/deadbeef.png")
    out = expand_message_images([msg], tmp_path)
    assert out[0]["content"][1]["image_url"]["url"] == "/images/deadbeef.png"


# --- replay_items display -----------------------------------------------------------


def test_replay_items_emits_user_image_item():
    messages = [_image_message("/images/abc.png")]
    items = replay_items(messages)
    # Both items carry the turn's message_index (see core/transcripts.py and
    # test_transcript_export.py's image-only-turn coverage), which is not what this test is about, so
    # membership is checked on the fields this test does care about rather than by exact dict equality.
    assert any(item["type"] == "user" and item["text"] == "look" for item in items)
    assert any(
        item["type"] == "image" and item["url"] == "/images/abc.png" and item["from"] == "user" for item in items
    )


def test_replay_items_emits_generated_image_from_tool_result():
    messages = [
        {"role": "tool", "name": "generate_image", "content": "Generated image (/images/gen.png).", "tool_call_id": "1"}
    ]
    items = replay_items(messages)
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


# --- image generation toolset -----------------------------------------------------------------


def test_image_toolset_gated_on_model_env(tmp_path, monkeypatch):
    from kokua.toolsets.image import build

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


def test_adopt_file_renames_a_written_file_to_its_content_address(tmp_path):
    source = tmp_path / "20261008-143424-874c499c.png"
    source.write_bytes(b"cat")
    reference = images.adopt_file(tmp_path, source)
    expected = hashlib.sha256(b"cat").hexdigest() + ".png"
    assert reference == "/images/" + expected
    assert (tmp_path / expected).read_bytes() == b"cat"
    assert not source.exists()


def test_adopt_file_drops_a_duplicate_and_keeps_the_stored_copy(tmp_path):
    first = tmp_path / "a.png"
    first.write_bytes(b"cat")
    images.adopt_file(tmp_path, first)
    second = tmp_path / "b.png"
    second.write_bytes(b"cat")
    reference = images.adopt_file(tmp_path, second)
    assert reference == "/images/" + hashlib.sha256(b"cat").hexdigest() + ".png"
    assert not second.exists()


def test_adopt_file_is_a_no_op_on_a_file_already_at_its_address(tmp_path):
    name = hashlib.sha256(b"cat").hexdigest() + ".png"
    (tmp_path / name).write_bytes(b"cat")
    assert images.adopt_file(tmp_path, tmp_path / name) == "/images/" + name
    assert (tmp_path / name).read_bytes() == b"cat"
