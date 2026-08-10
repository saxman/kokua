"""Shared channel doubles and the standard test config.

These live here rather than in one test module because several packages' tests need the same
fakes -- and because a test importing another test module makes the suite's layout load-bearing.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from aimu.aio import Channel
from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamingContentType

from kokua.config import AssistantConfig


class FakeChannel(Channel):
    name = "fake"

    def __init__(self, inbound: list[str] | None = None):
        self._inbound = inbound or []
        self.sent: list[str] = []

    async def receive(self) -> AsyncIterator[ChannelMessage]:
        for text in self._inbound:
            yield ChannelMessage(text=text, sender="fake", channel="fake")

    async def send(self, content, *, reply_to=None) -> None:
        if isinstance(content, str):
            self.sent.append(content)
            return
        parts = []
        async for chunk in content:
            if chunk.phase == StreamingContentType.GENERATING:
                parts.append(chunk.content)
        self.sent.append("".join(parts))


class _ConvCapturingChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.conversation_pushes: list[list] = []

    async def send_conversations(self, items):
        self.conversation_pushes.append(items)


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {
        # All leaf paths derive from data_dir; point it at the test's tmp dir.
        "data_dir": tmp_path,
        # Memory is on by default in real runs, but off here so the bulk of the tests stay fast and
        # hermetic (no ChromaDB init / state writes). The memory tests opt in with memory=True.
        "memory": False,
        # lean_supervisor defaults on in production, but most tests here assert the flat toolset (all
        # tools on the one agent), so pin flat; the lean-mode tests opt in with lean_supervisor=True.
        "lean_supervisor": False,
    }
    base.update(overrides)
    return AssistantConfig(**base)
