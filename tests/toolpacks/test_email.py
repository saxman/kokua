"""Tests for the built-in `email` tool-pack (send_email). Mock-only: no real SMTP server."""

from __future__ import annotations

import smtplib
from pathlib import Path

import pytest

from kokua.config import AssistantConfig
from kokua.toolpacks import email as email_pack
from kokua.toolpacks.email import _resolve_attachment, build

_FULL = {
    "email_host": "smtp.example.com",
    "email_port": 587,
    "email_from": "me@example.com",
    "email_to": "me@example.com",
}


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False, **_FULL}
    base.update(overrides)
    return AssistantConfig(**base)


class FakeSMTP:
    """Records the calls the tool makes, standing in for smtplib.SMTP / SMTP_SSL."""

    instances: list["FakeSMTP"] = []
    login_error: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append("login")
        self.username = username
        self.password = password
        if FakeSMTP.login_error is not None:
            raise FakeSMTP.login_error

    def send_message(self, message):
        self.calls.append("send_message")
        self.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.login_error = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


# --- gating ----------------------------------------------------------------------------------


def test_build_gated_on_host_recipient_and_password(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    assert [fn.__name__ for fn in build(_config(tmp_path))] == ["send_email"]

    monkeypatch.delenv("KOKUA_EMAIL_PASSWORD", raising=False)
    assert build(_config(tmp_path)) == []  # no password

    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    assert build(_config(tmp_path, email_host=None)) == []  # no host
    assert build(_config(tmp_path, email_to=None)) == []  # no recipient


# --- SMTP transport --------------------------------------------------------------------------


def test_send_uses_starttls_then_login_and_locks_recipient(tmp_path, monkeypatch, fake_smtp):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    send_email = build(_config(tmp_path))[0]

    result = send_email("Hello", "# Hi\n\nbody")

    assert "sent" in result.lower()
    server = fake_smtp.instances[-1]
    assert server.calls == ["starttls", "login", "send_message"]
    assert server.username == "me@example.com"
    message = server.sent[-1]
    assert message["To"] == "me@example.com"
    assert message["Subject"] == "Hello"
    assert message.get_content_type() == "multipart/alternative"


def test_send_ssl_skips_starttls(tmp_path, monkeypatch, fake_smtp):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    send_email = build(_config(tmp_path, email_use_ssl=True, email_port=465))[0]

    send_email("Subj", "body")

    assert fake_smtp.instances[-1].calls == ["login", "send_message"]  # no starttls


def test_subject_with_newline_is_rejected(tmp_path, monkeypatch, fake_smtp):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    send_email = build(_config(tmp_path))[0]

    result = send_email("bad\nInjected: header", "body")

    assert "single line" in result
    assert fake_smtp.instances == []  # never connected


# --- markdown rendering ----------------------------------------------------------------------


def test_body_has_plaintext_markdown_and_rendered_html(tmp_path, monkeypatch, fake_smtp):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    send_email = build(_config(tmp_path))[0]
    md = "# Title\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"

    send_email("Report", md)

    message = fake_smtp.instances[-1].sent[-1]
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert plain.strip() == md.strip()  # fallback is the raw markdown
    assert "<h1>" in html and "<table>" in html  # rendered


# --- attachments -----------------------------------------------------------------------------


def test_attachments_from_downloads_and_images(tmp_path, monkeypatch, fake_smtp):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    cfg = _config(tmp_path)
    cfg.downloads_path.mkdir(parents=True, exist_ok=True)
    cfg.images_path.mkdir(parents=True, exist_ok=True)
    (cfg.downloads_path / "report.pdf").write_bytes(b"%PDF-fake")
    (cfg.images_path / "chart.png").write_bytes(b"\x89PNG fake")
    send_email = build(cfg)[0]

    send_email("With files", "body", attachments=["report.pdf", "chart.png"])

    message = fake_smtp.instances[-1].sent[-1]
    assert message.get_content_type() == "multipart/mixed"
    attached = {part.get_filename(): part.get_payload(decode=True) for part in message.iter_attachments()}
    assert attached == {"report.pdf": b"%PDF-fake", "chart.png": b"\x89PNG fake"}


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "sub/x.txt", "", "missing.pdf"])
def test_invalid_attachment_sends_nothing(tmp_path, monkeypatch, fake_smtp, name):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "secret")
    send_email = build(_config(tmp_path))[0]

    result = send_email("Subj", "body", attachments=[name])

    assert "Nothing was sent" in result
    assert fake_smtp.instances == []  # fail closed: never connected


def test_resolve_attachment_rejects_paths_and_traversal(tmp_path):
    cfg = _config(tmp_path)
    cfg.downloads_path.mkdir(parents=True, exist_ok=True)
    (cfg.downloads_path / "ok.txt").write_text("hi")

    assert _resolve_attachment(cfg, "ok.txt")[0] == (cfg.downloads_path / "ok.txt").resolve()
    for bad in ["../secret", "/etc/passwd", "a/b.txt", ""]:
        assert _resolve_attachment(cfg, bad)[0] is None


# --- credential safety -----------------------------------------------------------------------


def test_auth_failure_leaks_neither_password_nor_server_text(tmp_path, monkeypatch, fake_smtp):
    monkeypatch.setenv("KOKUA_EMAIL_PASSWORD", "hunter2")
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad password hunter2")
    send_email = build(_config(tmp_path))[0]

    result = send_email("Subj", "body")

    assert "Failed to send email" in result
    assert "hunter2" not in result
    assert "5.7.8" not in result
    assert "SMTPAuthenticationError" in result


# --- discovery / landing on the agent --------------------------------------------------------


def test_email_toolpack_registered(monkeypatch):
    from kokua import plugins
    from kokua.plugins import ToolPack

    assert isinstance(email_pack.TOOL_PACK, ToolPack)
    packs = plugins.discover_tool_packs()
    assert packs.get("email") is email_pack.TOOL_PACK
