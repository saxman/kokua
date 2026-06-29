"""Tests for the entry-point plugin system (front ends + tool-packs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import MockAsyncModelClient
from mopai import plugins
from mopai.assistant import Assistant
from mopai.config import AssistantConfig
from mopai.plugins import FrontEnd, ToolPack


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False}
    base.update(overrides)
    return AssistantConfig(**base)


# --- Front-end discovery ---------------------------------------------------------------------


def test_builtin_frontends_discoverable():
    frontends = plugins.discover_frontends()
    assert {"cli", "web"} <= set(frontends)
    assert all(isinstance(fe, FrontEnd) for fe in frontends.values())
    assert callable(frontends["cli"].run)


def test_get_frontend_resolves_and_raises_on_unknown():
    assert plugins.get_frontend("cli").name == "cli"
    with pytest.raises(KeyError, match="unknown front end"):
        plugins.get_frontend("does-not-exist")


def test_builtin_frontends_available_without_entry_points(monkeypatch):
    # Even if entry-point metadata is empty (e.g. a bare source checkout), the hardcoded
    # built-in fallback still provides cli + web.
    monkeypatch.setattr(plugins, "_load_group", lambda group: {})
    frontends = plugins.discover_frontends()
    assert {"cli", "web"} <= set(frontends)


# --- Tool-pack discovery ---------------------------------------------------------------------


def test_example_tool_pack_discovered():
    packs = plugins.discover_tool_packs()
    assert "example" in packs
    assert isinstance(packs["example"], ToolPack)
    built = packs["example"].build(AssistantConfig())
    assert any(getattr(fn, "__name__", None) == "roll_dice" for fn in built)


async def test_tool_pack_tools_land_on_agent(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannelStub(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "roll_dice" in names  # contributed by the example tool-pack plugin


async def test_no_plugins_flag_omits_tool_pack_tools(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path, load_plugins=False), FakeChannelStub(), client=MockAsyncModelClient([])
    )
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "roll_dice" not in names


class FakeChannelStub:
    """Minimal Channel stand-in (Assistant.create doesn't touch the channel)."""

    name = "fake"

    async def receive(self):
        if False:
            yield None

    async def send(self, content, *, reply_to=None):
        pass
