"""A built-in tool-pack that lets the assistant email information to the user.

Contributes one tool, ``send_email``, that sends an email over SMTP (Python's stdlib ``smtplib`` +
``email.message.EmailMessage``, no extra dependency). The body is authored as Markdown and delivered as
a ``multipart/alternative`` message: an HTML part (rendered via the ``markdown`` package) with the raw
Markdown as the plain-text fallback.

Two deliberate design constraints, both security-relevant:

- The recipient is LOCKED to the configured ``email_to`` address. The tool takes no recipient argument,
  so the assistant can only ever email the user, never an arbitrary third party.
- The SMTP password is read from the ``KOKUA_EMAIL_PASSWORD`` environment variable, never from the TOML
  config (``settings.py`` has no ``[email].password`` key, so putting it there is a hard error).

Like ``image.py``, the pack self-gates: ``build`` returns no tool unless host + recipient + password are
all configured, so a default install never advertises an email tool it cannot fulfill. Sending is left
ungated (not in ``confirm_tools``) on purpose, so scheduled/proactive turns can send (e.g. a daily
digest); proactive turns auto-deny gated tools, which would otherwise block that use case.
"""

from __future__ import annotations

import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from aimu.tools import tool

from ..config import AssistantConfig
from ..plugins import ToolPack

# The SMTP password lives only in the environment, never in the config file. See module docstring.
_PASSWORD_ENV = "KOKUA_EMAIL_PASSWORD"


def _resolve_attachment(config: AssistantConfig, name: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve *name* to a file under downloads/ or images/, or return (None, reason).

    Traversal-safe: only a bare file name is accepted (``name == Path(name).name`` rejects paths, ``..``,
    and absolute names), and the resolved target must stay under the base folder even after following
    symlinks."""
    if not name or name != Path(name).name:
        return None, "must be a bare file name with no path"
    for base in (config.downloads_path, config.images_path):
        candidate = base / name
        try:
            resolved = candidate.resolve()
            resolved.relative_to(base.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved, None
    return None, "not found in the downloads or images folder"


def build(config: AssistantConfig) -> list:
    """Return the email tool when SMTP is fully configured, else nothing.

    Gated on host + recipient (``[email]`` config) and the ``KOKUA_EMAIL_PASSWORD`` env var, so a default
    install doesn't expose a send_email tool the model could call but never satisfy."""
    if not (config.email_host and config.email_to and os.environ.get(_PASSWORD_ENV)):
        return []

    @tool
    def send_email(subject: str, body_markdown: str, attachments: Optional[list] = None) -> str:
        """Send an email to the user (yourself). You cannot choose the recipient; it always goes to the
        user's own configured address. Use this to deliver digests, summaries, or reports.

        The body is written in Markdown and delivered as formatted HTML with a plain-text fallback.

        Args:
            subject: The email subject line (a single line of plain text).
            body_markdown: The email body as Markdown (headings, lists, tables, links, code).
            attachments: Optional list of file names to attach. Each must be a bare file name (no path)
                that already exists in the downloads or images folder; anything else is rejected.
        """
        import markdown

        if "\n" in subject or "\r" in subject:
            return "The subject must be a single line (no line breaks)."

        # Fail closed: resolve every attachment before connecting, so a bad name never results in a
        # partially-complete email the assistant believes carried the file.
        resolved: list[tuple[str, Path]] = []
        for name in attachments or []:
            path, reason = _resolve_attachment(config, name)
            if path is None:
                return f"Cannot attach {name!r}: it {reason}. Nothing was sent."
            resolved.append((name, path))

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = config.email_from or config.email_to
        message["To"] = config.email_to
        message.set_content(body_markdown)  # text/plain fallback = the raw Markdown
        html = markdown.markdown(body_markdown, extensions=["tables", "fenced_code", "sane_lists"])
        message.add_alternative(f"<!doctype html><html><body>{html}</body></html>", subtype="html")

        for name, path in resolved:
            mime, _ = mimetypes.guess_type(name)
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            message.add_attachment(
                path.read_bytes(), maintype=maintype, subtype=subtype or "octet-stream", filename=name
            )

        password = os.environ[_PASSWORD_ENV]
        username = config.email_username or config.email_from or config.email_to
        try:
            if config.email_use_ssl:
                with smtplib.SMTP_SSL(config.email_host, config.email_port, timeout=30) as server:
                    server.login(username, password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(config.email_host, config.email_port, timeout=30) as server:
                    server.starttls()
                    server.login(username, password)
                    server.send_message(message)
        except Exception as exc:
            # Only the exception class name: smtplib errors can echo server responses, and the password
            # must never reach the model's context or the session transcript.
            return f"Failed to send email: {type(exc).__name__}."
        return f"Email sent to you ({config.email_to}) with subject {subject!r}."

    return [send_email]


TOOL_PACK = ToolPack(
    name="email",
    description="Email yourself information (Markdown -> HTML) via SMTP; needs [email] config and KOKUA_EMAIL_PASSWORD.",
    build=build,
)
