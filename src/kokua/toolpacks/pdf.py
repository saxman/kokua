"""A built-in tool-pack that renders Markdown to a PDF file.

Contributes one tool, ``markdown_to_pdf``, that converts Markdown -> HTML (via the ``markdown``
package) -> PDF (via ``fpdf2``), saving into the downloads folder. Both libraries are pure Python
(no system libraries), so the pack works out of the box wherever Kokua is installed.

PDFs go in ``downloads_path`` rather than ``documents_path`` on purpose: the DocumentStore scans the
documents folder as UTF-8 text at startup, so a binary PDF there would break document loading. The web
front end serves the downloads folder at ``/download/<name>`` (see ``frontends/web.py``), so the tool's
result includes that relative link for the browser as well as the absolute path for the CLI.
"""

from __future__ import annotations

from pathlib import Path

from aimu.tools import tool

from ..config import AssistantConfig
from ..plugins import ToolPack

# fpdf2's built-in fonts (helvetica, ...) are Latin-1 only, so LLM-authored Markdown full of smart
# quotes / dashes would raise. Map the common offenders to ASCII, then drop anything else outside
# Latin-1. (Full Unicode would need a bundled TTF via FPDF.add_font -- out of scope here.)
_UNICODE_TO_ASCII = {
    "‘": "'",
    "’": "'",
    "‚": ",",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "--",
    "…": "...",
    " ": " ",
    "•": "-",
    "→": "->",
    "←": "<-",
    "≠": "!=",
    "≤": "<=",
    "≥": ">=",
    "×": "x",
    "−": "-",
    "·": "-",
}


def _latin1_safe(text: str) -> str:
    """Reduce *text* to Latin-1 so fpdf2's core fonts can render it (lossy for exotic characters)."""
    for uni, ascii_ in _UNICODE_TO_ASCII.items():
        text = text.replace(uni, ascii_)
    return text.encode("latin-1", "replace").decode("latin-1")


def _safe_pdf_name(filename: str) -> str:
    """Reduce *filename* to a bare basename ending in .pdf (never a path, never traversal)."""
    name = Path(filename or "").name or "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def build(config: AssistantConfig) -> list:
    """Return this pack's tools, bound to the configured downloads folder."""

    @tool
    def markdown_to_pdf(markdown_text: str, filename: str = "document.pdf") -> str:
        """Render Markdown text to a PDF file saved in the user's downloads folder.

        Args:
            markdown_text: The Markdown source to render (headings, lists, tables, fenced code, links).
            filename: Output file name; ".pdf" is appended if missing. Saved to the downloads folder.
        """
        import markdown
        from fpdf import FPDF

        name = _safe_pdf_name(filename)
        html = _latin1_safe(markdown.markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists"]))

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        pdf.write_html(html)

        config.downloads_path.mkdir(parents=True, exist_ok=True)
        out_path = config.downloads_path / name
        pdf.output(str(out_path))
        return f"Saved PDF to {out_path}. In the web UI, download it at /download/{name}."

    return [markdown_to_pdf]


TOOL_PACK = ToolPack(
    name="pdf",
    description="Render Markdown to a downloadable PDF saved in the documents folder (fpdf2 + markdown).",
    build=build,
)
