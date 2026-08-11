"""Shared channel doubles and the standard test config.

These live here rather than in one test module because several packages' tests need the same
fakes -- and because a test importing another test module makes the suite's layout load-bearing.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

from aimu.aio import Channel
from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamingContentType

from kokua.config import AssistantConfig
from kokua.config import file as settings


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


class SubagentCapturingChannel(FakeChannel):
    """A channel that records ``subagent`` frames, with the display flags a reporter reads."""

    def __init__(self, *, show_thinking: bool = False, show_tools: bool = False):
        super().__init__()
        self.show_thinking = show_thinking
        self.show_tools = show_tools
        self.subagent_frames: list[dict] = []

    async def send_subagent(self, event: dict) -> None:
        self.subagent_frames.append(event)


@lru_cache(maxsize=1)
def _shipped_roles() -> dict[str, dict]:
    return tomllib.loads(settings.example_text())["subagents"]["roles"]


def example_subagent_roles() -> dict[str, dict]:
    """The sub-agent roles Kokua ships in config.example.toml, parsed from that file.

    Read rather than copied on purpose: roles live only in config.toml now, so a Python copy here
    would drift from the shipped ones and the role tests would stop describing what users run.
    Returns fresh dicts so a caller mutating a role cannot corrupt another test's config.
    """
    return {name: dict(spec) for name, spec in _shipped_roles().items()}


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
        # AssistantConfig defaults to no roles, which is the *unconfigured* install (spawn_subagent
        # falls back to one untyped worker). A normal install has the roles config.example.toml ships,
        # so mirror that here; the fallback tests opt out with subagent_roles={}.
        "subagent_roles": example_subagent_roles(),
    }
    base.update(overrides)
    return AssistantConfig(**base)
