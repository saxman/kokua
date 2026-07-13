"""Kokua's terminal channel: AIMU's ``CLIChannel`` plus an ``/attach`` command for image input.

The terminal can't render images, so this stays minimal: ``/attach <path>`` stages a local image file
onto the next message (``ChannelMessage.images``), which the model then reads. Generated images are
reported by the assistant as an ``/images/<name>`` reference into ``images_path``; the file lives under
``$KOKUA_HOME/data/images``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

from aimu.aio import CLIChannel as BaseCLIChannel
from aimu.aio.channels.base import ChannelMessage

_ATTACH_PREFIX = "/attach "


class CLIChannel(BaseCLIChannel):
    """AIMU's ``CLIChannel`` with an ``/attach <path>`` command that stages images onto the next turn."""

    async def receive(self) -> AsyncIterator[ChannelMessage]:
        pending: list[str] = []
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
            if pending:
                message.images = pending
                pending = []
            yield message
