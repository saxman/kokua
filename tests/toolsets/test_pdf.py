"""Tests for the built-in `pdf` toolset (markdown_to_pdf)."""

from __future__ import annotations

from pathlib import Path

from kokua import plugins
from kokua.config import AssistantConfig
from tests.channels import example_subagent_roles
from kokua.plugins import Toolset
from kokua.toolsets import LiveState, ToolsetContext
from kokua.toolsets.pdf import _safe_pdf_name, build


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False, "subagent_roles": example_subagent_roles()}
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


def test_pdf_toolset_discovered():
    toolsets = plugins.discover_toolsets()
    assert "pdf" in toolsets
    assert isinstance(toolsets["pdf"], Toolset)
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig()), agent=None)
    built = toolsets["pdf"].build(ctx)
    assert any(getattr(fn, "__name__", None) == "markdown_to_pdf" for fn in built)


def test_markdown_to_pdf_reaches_a_role_that_names_the_pack(tmp_path):
    """The supervisor mounts no pack tools, so a role has to name `pdf` for the tool to reach a worker."""
    from kokua.core.build import _build_subagent_agent_types, _load_plugin_tools_by_pack

    cfg = _config(tmp_path, subagent_roles={"writer": {"description": "Writes.", "tool_packs": ["pdf"]}})
    types = _build_subagent_agent_types(cfg, [], _load_plugin_tools_by_pack(cfg))
    assert "markdown_to_pdf" in {fn.__name__ for fn in types["writer"]["tools"]}


def test_markdown_to_pdf_writes_valid_pdf(tmp_path):
    cfg = _config(tmp_path)
    tool = build(cfg)[0]
    md = (
        "# Title\n\nSome **bold** text with a smart quote: “hi” — and a dash.\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n```py\nprint('x')\n```\n\n- one\n- two\n"
    )
    result = tool(md, "report")
    out = cfg.downloads_path / "report.pdf"
    assert out.is_file()
    assert out.read_bytes().startswith(b"%PDF-")  # a real PDF, and Unicode did not raise
    assert not (cfg.documents_path / "report.pdf").exists()  # never written into the DocumentStore dir
    assert "/download/report.pdf" in result  # surfaces the web download link


def test_safe_pdf_name_appends_extension_and_strips_paths():
    assert _safe_pdf_name("notes") == "notes.pdf"
    assert _safe_pdf_name("notes.pdf") == "notes.pdf"
    assert _safe_pdf_name("report.PDF") == "report.PDF"  # already a .pdf (case-insensitive)
    assert _safe_pdf_name("../../etc/passwd") == "passwd.pdf"  # reduced to a bare basename
    assert _safe_pdf_name("") == "document.pdf"
