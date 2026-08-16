# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "markdown>=3.5",
# ]
# ///
"""Email a Markdown body to the address the host configured.

Every setting arrives in the environment, which the host populates from its own configuration. That is
deliberate: re-deriving Kokua's config here would duplicate its path and settings resolution and drift
from it, and the recipient in particular must not be something a caller can choose.

  KOKUA_EMAIL_HOST      SMTP host (required)
  KOKUA_EMAIL_TO        the only recipient this can ever send to (required)
  KOKUA_EMAIL_PASSWORD  SMTP password (required)
  KOKUA_EMAIL_PORT      default 587
  KOKUA_EMAIL_USERNAME  login user; falls back to FROM, then TO
  KOKUA_EMAIL_FROM      From: header; falls back to TO
  KOKUA_EMAIL_USE_SSL   "1" for implicit TLS (usually port 465), else STARTTLS
  KOKUA_DOWNLOADS_DIR   folder an attachment may come from
  KOKUA_IMAGES_DIR      folder an attachment may come from
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path


def resolve_attachment(name: str) -> tuple[Path | None, str]:
    """Resolve a bare file name to a path in the downloads or images folder, or explain why not."""
    if name != Path(name).name:
        return None, "must be a bare file name, not a path"
    for key in ("KOKUA_DOWNLOADS_DIR", "KOKUA_IMAGES_DIR"):
        base = os.environ.get(key)
        if not base:
            continue
        candidate = Path(base) / name
        if candidate.is_file():
            return candidate, ""
    return None, "was not found in the downloads or images folder"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Email a Markdown body to the user. The recipient is fixed by the host.",
        epilog=(
            "Examples:\n"
            '  uv run scripts/send.py --subject "Weekly review" --body-file body.md\n'
            '  uv run scripts/send.py --subject "Report" --body-file body.md --attach report.pdf\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--subject", required=True, help="Subject line; must be a single line.")
    parser.add_argument("--body-file", required=True, help="Path to a Markdown file holding the body.")
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="NAME",
        help="Bare file name in the downloads or images folder. Repeatable.",
    )
    args = parser.parse_args()

    host = os.environ.get("KOKUA_EMAIL_HOST")
    to = os.environ.get("KOKUA_EMAIL_TO")
    password = os.environ.get("KOKUA_EMAIL_PASSWORD")
    if not (host and to and password):
        print(
            "Error: email is not configured. The host must set KOKUA_EMAIL_HOST, KOKUA_EMAIL_TO and "
            "KOKUA_EMAIL_PASSWORD. Nothing was sent.",
            file=sys.stderr,
        )
        return 2

    if "\n" in args.subject or "\r" in args.subject:
        print("Error: --subject must be a single line.", file=sys.stderr)
        return 2

    body_path = Path(args.body_file)
    if not body_path.is_file():
        print(f"Error: --body-file {args.body_file!r} is not a file.", file=sys.stderr)
        return 2

    # Fail closed: resolve every attachment before connecting, so a bad name never leaves a
    # partially-sent email the caller believes carried the file.
    resolved: list[tuple[str, Path]] = []
    for name in args.attach:
        path, reason = resolve_attachment(name)
        if path is None:
            print(f"Error: cannot attach {name!r}: it {reason}. Nothing was sent.", file=sys.stderr)
            return 2
        resolved.append((name, path))

    import markdown

    body = body_path.read_text(encoding="utf-8")
    message = EmailMessage()
    message["Subject"] = args.subject
    message["From"] = os.environ.get("KOKUA_EMAIL_FROM") or to
    message["To"] = to
    message.set_content(body)  # text/plain fallback is the raw Markdown
    html = markdown.markdown(body, extensions=["tables", "fenced_code", "sane_lists"])
    message.add_alternative(f"<!doctype html><html><body>{html}</body></html>", subtype="html")

    for name, path in resolved:
        mime, _ = mimetypes.guess_type(name)
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype or "octet-stream", filename=name)

    port = int(os.environ.get("KOKUA_EMAIL_PORT") or 587)
    username = os.environ.get("KOKUA_EMAIL_USERNAME") or os.environ.get("KOKUA_EMAIL_FROM") or to
    use_ssl = os.environ.get("KOKUA_EMAIL_USE_SSL") == "1"
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(message)
    except Exception as exc:
        # Only the exception class name: smtplib errors can echo server responses, and the password
        # must never reach the caller's context or the session transcript.
        print(f"Error: failed to send email: {type(exc).__name__}.", file=sys.stderr)
        return 1

    print(f"Email sent to you ({to}) with subject {args.subject!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
