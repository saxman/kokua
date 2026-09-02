"""Kokua's terminal channel: AIMU's ``CLIChannel`` plus the two commands a terminal needs of its own.

Only two, and that is the point of the boundary. ``/stop``, ``/diag``, and the conversation commands
(``/new``, ``/conversations``, ``/switch``) are typed here but handled in ``Assistant._serve_channel``,
because what they act on is core state a channel has no route to. What is left for this module is the
pair that is genuinely about the terminal: making up for what it cannot draw, and offering by typing
what the web composer offers as a control.

The terminal can't render images, so ``/attach <path>`` stages a local image file onto the next message
(``ChannelMessage.images``), which the model then reads. Generated images are reported by the assistant as
an ``/images/<name>`` reference into ``images_path``; the file lives under ``$KOKUA_HOME/data/images``.

``/think <level>`` is the terminal's half of the per-turn reasoning effort the web composer offers as a
picker. It rides ``ChannelMessage.metadata``, which is the transport-neutral seam the core reads, so
neither channel is a special case there.

The two commands stage differently on purpose. An attachment is consumed by the message it rides, since
sending the same image again is almost never what someone meant. An effort holds until it is changed,
matching the composer's picker: "think hard for a while" is the request people actually have, and a
one-shot version would mean retyping the level on every message.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator, Optional

from aimu.aio import CLIChannel as BaseCLIChannel
from aimu.aio.channels.base import ChannelMessage

_ATTACH_PREFIX = "/attach "
_THINK_COMMAND = "/think"
# The words `/think` takes, in the order they are offered. "default" is the one that is not a level: it
# clears the request, leaving whatever config.toml declares in force.
_THINK_CHOICES = ("default", "off", "low", "medium", "high")


def _apply_think(argument: str, current: Optional[str]) -> Optional[str]:
    """Read one ``/think`` argument and report what it did; return the effort to carry from here on.

    ``argument`` arrives already stripped, so a bare ``/think`` and a ``/think`` with trailing spaces are
    the same query rather than one query and one rejected empty level.

    Returns ``current`` unchanged for that query and for an unrecognized word, so a typo costs a message
    of feedback rather than the effort the user had already set.
    """
    if not argument:
        print(f"[think] {current or 'default'}", flush=True)
        return current
    choice = argument.lower()
    if choice == "default":
        print("[think] default (the effort your config declares)", flush=True)
        return None
    if choice in _THINK_CHOICES:
        print(f"[think] {choice} (every message from here, until /think default)", flush=True)
        return choice
    print(f"[think] not a level: {argument!r}. Try: {', '.join(_THINK_CHOICES)}", file=sys.stderr, flush=True)
    return current


class CLIChannel(BaseCLIChannel):
    """AIMU's ``CLIChannel`` with ``/attach <path>`` and ``/think <level>``."""

    async def receive(self) -> AsyncIterator[ChannelMessage]:
        pending: list[str] = []
        thinking: Optional[str] = None
        async for message in super().receive():
            text = message.text
            if text.startswith(_ATTACH_PREFIX):
                path = Path(text[len(_ATTACH_PREFIX) :].strip()).expanduser()
                if path.is_file():
                    pending.append(str(path))
                    print(f"[attached] {path.name} (sent with your next message)", flush=True)
                else:
                    print(f"[attach] no such file: {path}", file=sys.stderr, flush=True)
                continue
            stripped = text.strip()
            if stripped == _THINK_COMMAND or stripped.startswith(_THINK_COMMAND + " "):
                thinking = _apply_think(stripped[len(_THINK_COMMAND) :].strip(), thinking)
                continue
            if pending:
                message.images = pending
                pending = []
            if thinking is not None:
                # A fresh dict rather than a mutation, so nothing this channel does can be seen by a
                # message object its caller still holds.
                message.metadata = {**(message.metadata or {}), "thinking": thinking}
            yield message
