"""The image toolset: the env gate and the generator's saved-reference return.

The generator path was previously untested -- only the gate was, and the module named `images.py`
(the on-disk store) has a similar test module name, which made the gap easy to miss.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from kokua.config import AssistantConfig
from kokua.toolsets.image import build


def _config(tmp_path: Path) -> AssistantConfig:
    return AssistantConfig(data_dir=tmp_path)


class _Chunk:
    def __init__(self, content):
        self.content = content


def _drain(generator):
    """Run a tool generator to completion, returning (yielded chunks, the tool's return value).

    The tool yields progress chunks and *returns* its result, so the result arrives as
    StopIteration.value -- it is not the last yielded item.
    """
    chunks = []
    while True:
        try:
            chunks.append(next(generator))
        except StopIteration as done:
            return chunks, done.value


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


def test_generate_image_streams_progress_and_returns_a_servable_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "gemini:nano-banana")
    config = _config(tmp_path)
    written = config.images_path / "out.png"

    def generate(prompt, *, format, output_dir, stream):
        assert prompt == "a cat" and format == "path" and stream is True
        assert Path(output_dir) == config.images_path  # saved where the web server can serve it
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(b"png")
        yield _Chunk({"progress": 0.5})
        yield _Chunk({"final": True, "result": str(written)})

    _install_fake_aimu(monkeypatch, generate)
    (generate_image,) = build(config)

    chunks, result = _drain(generate_image("a cat"))
    # The denoising chunks flow through to the UI; the reference is the tool's return value.
    assert [chunk.content for chunk in chunks] == [{"progress": 0.5}, {"final": True, "result": str(written)}]
    assert result == "Generated image, shown to the user inline (/images/out.png)."


def test_generate_image_reports_no_output(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "gemini:nano-banana")

    def generate(prompt, *, format, output_dir, stream):
        yield _Chunk({"progress": 1.0})  # never signals final

    _install_fake_aimu(monkeypatch, generate)
    (generate_image,) = build(_config(tmp_path))
    assert _drain(generate_image("a cat"))[1] == "Image generation produced no output."


def test_generate_image_reports_an_unbuildable_client(tmp_path, monkeypatch):
    """A misconfigured image model must come back as a message the model can relay, not a crash."""
    monkeypatch.setenv("AIMU_IMAGE_MODEL", "bogus:model")
    fake = types.ModuleType("aimu")

    def explode():
        raise ValueError("unknown image model 'bogus:model'")

    fake.image_client = explode
    monkeypatch.setitem(sys.modules, "aimu", fake)

    (generate_image,) = build(_config(tmp_path))
    chunks, result = _drain(generate_image("a cat"))
    assert chunks == []  # it never reaches the generator loop
    assert "unavailable" in result and "AIMU_IMAGE_MODEL" in result
