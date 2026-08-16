---
name: markdown-to-pdf
description: Render Markdown to a PDF the user can download. Use when asked to produce a PDF, a report, or a printable document, or when the user says "as a PDF".
license: Apache-2.0
compatibility: Requires uv (the script declares its own dependencies inline). Reads KOKUA_DOWNLOADS_DIR.
metadata:
  author: kokua
---

# Markdown to PDF

Renders Markdown to a PDF saved in the user's downloads folder, where Kokua's web UI serves it.

## Steps

1. Write the Markdown to a file. Do not try to pass a document as a command-line argument: it will
   be too long and the quoting will break.

2. Run the script:

   ```bash
   uv run scripts/render.py --input <markdown-file> --name <output.pdf>
   ```

3. Report the `/download/<name>` link the script prints, so the user can click it in the web UI.

## Notes

- `--name` is reduced to a bare filename ending in `.pdf`, so a path or `..` in it is ignored.
- The PDF is written to `$KOKUA_DOWNLOADS_DIR`, which Kokua sets. Without it the script writes to
  the current directory and says so.
- Fonts are the fpdf2 built-ins, which are Latin-1 only. Smart quotes, dashes and arrows are folded
  to ASCII; anything else outside Latin-1 becomes `?`. That is lossy and expected.
- Run `uv run scripts/render.py --help` for the full interface.
