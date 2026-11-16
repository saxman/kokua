"""The image toolset: the env gate, the generator that publishes its image to the turn, and refining an
earlier image by passing it as a reference."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
import threading
import types
from pathlib import Path

from kokua import images
from kokua.config import AssistantConfig
from kokua.toolsets.image import build


def _config(tmp_path: Path) -> AssistantConfig:
    return AssistantConfig(data_dir=tmp_path)


class _Chunk:
    def __init__(self, content):
        self.content = content


def _drain(agen):
    """Run an async tool generator to completion, returning its chunks' contents."""

    async def go():
        return [chunk.content async for chunk in agen]

    return asyncio.run(go())


def _install_fake_aimu(monkeypatch, generate):
    """Stand in for aimu.image_client(), which would otherwise need a real image model."""
    fake = types.ModuleType("aimu")
    fake.image_client = lambda: types.SimpleNamespace(generate=generate)
    monkeypatch.setitem(sys.modules, "aimu", fake)


def test_no_tools_without_an_image_model(tmp_path, monkeypatch):
    monkeypatch.delenv("AIMU_IMAGE_MODEL", raising=False)
    assert build(_config(tmp_path)) == []


def test_tool_offered_when_an_image_model_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "gemini:nano-banana")
    assert [fn.__name__ for fn in build(_config(tmp_path))] == ["generate_image"]


def test_generate_image_publishes_the_image_and_tells_the_model(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "gemini:nano-banana")
    config = _config(tmp_path)
    written = config.images_path / "20261008-143424-874c499c.png"

    def generate(prompt, *, format, output_dir, stream):
        assert format == "path" and stream is True
        assert Path(output_dir) == config.images_path  # saved where the web server can serve it
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(b"png")
        yield _Chunk({"progress": 0.5})
        yield _Chunk({"final": True, "result": str(written)})

    published = []

    async def fake_publish(url, *, filename, mime, path):
        published.append((url, filename, mime, path))
        return True

    _install_fake_aimu(monkeypatch, generate)
    monkeypatch.setattr("kokua.core.artifacts.publish", fake_publish)
    (generate_image,) = build(config)

    contents = _drain(generate_image("A photorealistic cat, sitting"))
    reference = "/images/" + hashlib.sha256(b"png").hexdigest() + ".png"
    assert published == [
        (reference, "a-photorealistic-cat-sitting.png", "image/png", config.images_path / reference.rsplit("/", 1)[1])
    ]
    assert contents[0] == {"progress": 0.5}
    result = contents[-1]["result"]
    assert contents[-1]["final"] is True
    assert str(result) == f"Generated image, shown to the user ({reference})."
    # The pixels go back to the model too, so a vision model can see what it made.
    assert result.images == ("data:image/png;base64," + base64.b64encode(b"png").decode("ascii"),)


def test_generate_image_reports_no_output(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "gemini:nano-banana")

    def generate(prompt, *, format, output_dir, stream):
        yield _Chunk({"progress": 1.0})  # never signals final

    _install_fake_aimu(monkeypatch, generate)
    (generate_image,) = build(_config(tmp_path))
    assert _drain(generate_image("a cat"))[-1] == {"final": True, "result": "Image generation produced no output."}


def test_generate_image_builds_the_client_off_the_event_loop_thread(tmp_path, monkeypatch):
    """Building a client can import torch and load weights; on the loop thread it would stall every conversation."""
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "gemini:nano-banana")
    built_on_main_thread = []

    def generate(prompt, *, format, output_dir, stream):
        yield _Chunk({"progress": 1.0})

    fake = types.ModuleType("aimu")

    def image_client():
        built_on_main_thread.append(threading.current_thread() is threading.main_thread())
        return types.SimpleNamespace(generate=generate)

    fake.image_client = image_client
    monkeypatch.setitem(sys.modules, "aimu", fake)

    (generate_image,) = build(_config(tmp_path))
    _drain(generate_image("a cat"))
    assert built_on_main_thread == [False]


def test_generate_image_reports_an_unbuildable_client(tmp_path, monkeypatch):
    """A misconfigured image model must come back as a message the model can relay, not a crash."""
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "bogus:model")
    fake = types.ModuleType("aimu")

    def explode():
        raise ValueError("unknown image model 'bogus:model'")

    fake.image_client = explode
    monkeypatch.setitem(sys.modules, "aimu", fake)

    (generate_image,) = build(_config(tmp_path))
    contents = _drain(generate_image("a cat"))
    assert len(contents) == 1  # it never reaches the generator loop
    assert "unavailable" in contents[0]["result"] and "AIMU_IMAGE_MODEL" in contents[0]["result"]


# --- refining an earlier image ------------------------------------------------------------------


def _parameters(tool) -> list[str]:
    return list(tool.__tool_spec__["function"]["parameters"]["properties"])


def _shaped(monkeypatch, starts_from_image, honours_strength):
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "hf:some/model")
    monkeypatch.setattr("kokua.toolsets.image.reference_support", lambda model: (starts_from_image, honours_strength))


def test_the_tool_offers_only_what_the_image_model_honours(tmp_path, monkeypatch):
    _shaped(monkeypatch, False, False)
    assert _parameters(build(_config(tmp_path))[0]) == ["prompt"]
    _shaped(monkeypatch, True, False)
    assert _parameters(build(_config(tmp_path))[0]) == ["prompt", "reference_image"]
    _shaped(monkeypatch, True, True)
    assert _parameters(build(_config(tmp_path))[0]) == ["prompt", "reference_image", "strength"]


def test_the_description_says_a_reference_cannot_zoom_out(tmp_path, monkeypatch):
    """ "Show me more of it" is the refinement a user most naturally asks for, and image-to-image cannot
    do it; saying so lets the model set the expectation instead of discovering it after a minute."""
    _shaped(monkeypatch, True, True)
    description = build(_config(tmp_path))[0].__tool_spec__["function"]["description"]
    assert "cannot zoom out" in description


def _stored(config) -> str:
    """Store an image the way an upload or a generation is stored, and return its reference."""
    return images.save_data_url(config.images_path, "data:image/png;base64," + base64.b64encode(b"ref").decode("ascii"))


def test_a_reference_and_strength_reach_the_image_client(tmp_path, monkeypatch):
    _shaped(monkeypatch, True, True)
    config = _config(tmp_path)
    reference = _stored(config)
    received = {}

    def generate(prompt, *, format, output_dir, stream, **options):
        received.update(options)
        yield _Chunk({"progress": 1.0})

    _install_fake_aimu(monkeypatch, generate)
    (generate_image,) = build(config)
    _drain(generate_image("the same giraffe, in watercolour", reference_image=reference, strength=0.4))

    assert received == {"reference_image": images.reference_to_path(config.images_path, reference), "strength": 0.4}


def test_no_reference_means_no_reference_options(tmp_path, monkeypatch):
    _shaped(monkeypatch, True, True)
    received = {}

    def generate(prompt, *, format, output_dir, stream, **options):
        received.update(options)
        yield _Chunk({"progress": 1.0})

    _install_fake_aimu(monkeypatch, generate)
    (generate_image,) = build(_config(tmp_path))
    _drain(generate_image("a giraffe"))
    assert received == {}


def test_an_unknown_or_escaping_reference_is_answered_not_generated(tmp_path, monkeypatch):
    _shaped(monkeypatch, True, True)
    calls = []

    def generate(prompt, **kwargs):
        calls.append(prompt)
        yield _Chunk({"progress": 1.0})

    _install_fake_aimu(monkeypatch, generate)
    (generate_image,) = build(_config(tmp_path))
    for reference in ("/images/nope.png", "/images/../config.toml", "/etc/passwd"):
        (answer,) = _drain(generate_image("x", reference_image=reference))
        assert "There is no stored image" in answer["result"]
    assert calls == []


def test_a_strength_out_of_range_is_answered(tmp_path, monkeypatch):
    _shaped(monkeypatch, True, True)
    config = _config(tmp_path)
    reference = _stored(config)
    _install_fake_aimu(monkeypatch, lambda prompt, **kwargs: iter(()))
    (generate_image,) = build(config)
    (answer,) = _drain(generate_image("x", reference_image=reference, strength=1.5))
    assert "strength must be greater than 0 and at most 1" in answer["result"]


def test_reference_support_reads_aimus_catalogue():
    """The configured SD 1.5 has an image-to-image pipeline that takes strength; an uncatalogued model is
    treated as taking neither, so nothing is offered that cannot be honoured."""
    from kokua.toolsets.image import reference_support

    reference_support.cache_clear()
    assert reference_support("hf:runwayml/stable-diffusion-v1-5") == (True, True)
    assert reference_support("hf:someone/not-catalogued") == (False, False)
