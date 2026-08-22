"""The terminal channel's own commands: `/think`, and its interaction with `/attach`.

`/attach` has its own coverage in `tests/test_images.py`, where it belongs with the rest of the image
path; what is here is the reasoning-effort command and the two commands sharing one receive loop.
"""

from __future__ import annotations

import asyncio
import io

from kokua.channels.cli import CLIChannel


def _messages(stdin: str, monkeypatch) -> list:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    async def run():
        return [m async for m in CLIChannel().receive()]

    return asyncio.run(run())


def test_think_puts_the_level_on_every_message_that_follows(monkeypatch):
    """Sticky, unlike `/attach`: the web composer's picker holds until it is changed, and a terminal that
    made the user retype the level every message would be a different feature wearing the same name."""
    messages = _messages("/think high\nfirst\nsecond\n", monkeypatch)

    assert [m.text for m in messages] == ["first", "second"]
    assert [m.metadata["thinking"] for m in messages] == ["high", "high"]


def test_think_default_puts_the_configured_effort_back(monkeypatch):
    messages = _messages("/think high\nfirst\n/think default\nsecond\n", monkeypatch)

    assert messages[0].metadata["thinking"] == "high"
    assert "thinking" not in messages[1].metadata


def test_a_message_carries_no_effort_until_one_is_asked_for(monkeypatch):
    messages = _messages("hello\n", monkeypatch)

    assert "thinking" not in messages[0].metadata


def test_an_unrecognized_level_changes_nothing_and_says_so(monkeypatch, capsys):
    messages = _messages("/think high\n/think xhigh\nfirst\n", monkeypatch)

    assert messages[0].metadata["thinking"] == "high", "a rejected level must not clear the good one"
    assert "xhigh" in capsys.readouterr().err


def test_a_bare_think_reports_the_current_level(monkeypatch, capsys):
    messages = _messages("/think medium\n/think\nfirst\n", monkeypatch)

    assert messages[0].metadata["thinking"] == "medium"
    assert "medium" in capsys.readouterr().out


def test_think_and_attach_both_apply_to_the_same_message(tmp_path, monkeypatch):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG fake")

    messages = _messages(f"/think low\n/attach {image}\nwhat is this?\n", monkeypatch)

    assert len(messages) == 1
    assert messages[0].images == [str(image)]
    assert messages[0].metadata["thinking"] == "low"
