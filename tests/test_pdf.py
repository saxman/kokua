"""Tests for the built-in `pdf` tool-pack (markdown_to_pdf)."""

from __future__ import annotations

from pathlib import Path

from helpers import MockAsyncModelClient
from kokua import plugins
from kokua.assistant import Assistant
from kokua.config import AssistantConfig
from kokua.plugins import ToolPack
from kokua.toolpacks.pdf import _safe_pdf_name, build


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False}
    base.update(overrides)
    return AssistantConfig(**base)


class FakeChannelStub:
    """Minimal Channel stand-in (Assistant.create doesn't touch the channel)."""

    name = "fake"

    async def receive(self):
        if False:
            yield None

    async def send(self, content, *, reply_to=None):
        pass


def test_pdf_tool_pack_discovered():
    packs = plugins.discover_tool_packs()
    assert "pdf" in packs
    assert isinstance(packs["pdf"], ToolPack)
    built = packs["pdf"].build(AssistantConfig())
    assert any(getattr(fn, "__name__", None) == "markdown_to_pdf" for fn in built)


async def test_markdown_to_pdf_lands_on_agent(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannelStub(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "markdown_to_pdf" in names


def test_markdown_to_pdf_writes_valid_pdf(tmp_path):
    cfg = _config(tmp_path)
    tool = build(cfg)[0]
    md = (
        "# Title\n\nSome **bold** text with a smart quote: “hi” — and a dash.\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n```py\nprint('x')\n```\n\n- one\n- two\n"
    )
    result = tool(md, "report")
    out = cfg.documents_path / "report.pdf"
    assert out.is_file()
    assert out.read_bytes().startswith(b"%PDF-")  # a real PDF, and Unicode did not raise
    assert "/download/report.pdf" in result  # surfaces the web download link


def test_safe_pdf_name_appends_extension_and_strips_paths():
    assert _safe_pdf_name("notes") == "notes.pdf"
    assert _safe_pdf_name("notes.pdf") == "notes.pdf"
    assert _safe_pdf_name("report.PDF") == "report.PDF"  # already a .pdf (case-insensitive)
    assert _safe_pdf_name("../../etc/passwd") == "passwd.pdf"  # reduced to a bare basename
    assert _safe_pdf_name("") == "document.pdf"
