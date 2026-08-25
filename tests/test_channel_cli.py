"""The terminal channel's own commands: `/think`, and its interaction with `/attach`.

`/attach` has its own coverage in `tests/test_images.py`, where it belongs with the rest of the image
path; what is here is the reasoning-effort command and the two commands sharing one receive loop, plus
the one AIMU default this channel is built on rather than around.
"""

from __future__ import annotations

import asyncio
import io

from kokua.channels.cli import _THINK_CHOICES, CLIChannel
from kokua.config.file import thinking_request


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


def test_every_offered_level_but_default_is_a_word_the_core_accepts():
    """`_THINK_CHOICES` is its own list on purpose (`/think` offers "default", a word `thinking_request`
    has no case for), but that means nothing here catches the two drifting apart otherwise: a level
    added to one vocabulary and not the other would offer a `/think` choice that silently falls back to
    the configured effort instead of the one just picked."""
    for level in _THINK_CHOICES:
        if level == "default":
            continue
        assert thinking_request(level) is not None


def test_a_bare_channel_streams_reasoning_and_tool_calls():
    """Kokua has no display settings: the terminal shows the loop because AIMU's channel relays those two
    phases unless told not to. Nothing in Kokua passes the flags, so this default is load-bearing, and an
    AIMU that flipped it back would silently reduce Kokua to answers only (see `kokua.aimu_compat`)."""
    channel = CLIChannel()
    assert channel.stream_thinking is True
    assert channel.stream_tools is True
