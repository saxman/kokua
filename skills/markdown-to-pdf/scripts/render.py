# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fpdf2>=2.7",
#   "markdown>=3.5",
# ]
# ///
"""Render a Markdown file to a PDF in Kokua's downloads folder.

Dependencies are declared inline (PEP 723) so this skill carries its own, rather than requiring the
host to install fpdf2 and markdown. Run it with `uv run`, which resolves them into an isolated
environment.

Output location comes from KOKUA_DOWNLOADS_DIR rather than being derived from Kokua's config: the host
knows where it serves downloads from, and re-deriving that here would duplicate config resolution and
drift from it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# fpdf2's built-in fonts are Latin-1 only, so model-authored Markdown full of smart quotes and dashes
# would raise. Fold the common offenders to ASCII, then replace anything else outside Latin-1. Full
# Unicode would need a bundled TTF via FPDF.add_font.
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


def latin1_safe(text: str) -> str:
    """Reduce text to Latin-1 so fpdf2's core fonts can render it. Lossy for exotic characters."""
    for uni, ascii_ in _UNICODE_TO_ASCII.items():
        text = text.replace(uni, ascii_)
    return text.encode("latin-1", "replace").decode("latin-1")


def safe_pdf_name(filename: str) -> str:
    """Reduce filename to a bare basename ending in .pdf, so it can never be a path or traversal."""
    name = Path(filename or "").name or "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a Markdown file to a PDF in the downloads folder.",
        epilog=(
            "Examples:\n"
            "  uv run scripts/render.py --input report.md --name weekly-report.pdf\n"
            "  uv run scripts/render.py --input notes.md\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="Path to the Markdown file to render.")
    parser.add_argument(
        "--name",
        default="document.pdf",
        help="Output file name; '.pdf' is appended if missing. Any path is stripped. (default: document.pdf)",
    )
    args = parser.parse_args()

    source = Path(args.input)
    if not source.is_file():
        print(f"Error: --input {args.input!r} is not a file.", file=sys.stderr)
        return 2

    import markdown
    from fpdf import FPDF

    name = safe_pdf_name(args.name)
    html = latin1_safe(
        markdown.markdown(
            source.read_text(encoding="utf-8"),
            extensions=["tables", "fenced_code", "sane_lists"],
        )
    )

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.write_html(html)

    downloads = os.environ.get("KOKUA_DOWNLOADS_DIR")
    if downloads:
        out_dir = Path(downloads)
        served = f" In the web UI, download it at /download/{name}."
    else:
        out_dir = Path.cwd()
        served = " KOKUA_DOWNLOADS_DIR was not set, so this is not in the served downloads folder."
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    pdf.output(str(out_path))

    print(f"Saved PDF to {out_path}.{served}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
