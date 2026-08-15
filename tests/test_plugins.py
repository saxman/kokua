"""Tests for the entry-point plugin system (front ends + toolsets)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import MockAsyncModelClient
from kokua import plugins
from kokua.core.assistant import Assistant
from kokua.config import AssistantConfig
from tests.channels import example_subagent_roles
from kokua.plugins import FrontEnd, Toolset
from kokua.toolsets import LiveState, ToolsetContext


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False, "subagent_roles": example_subagent_roles()}
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


# --- Toolset discovery ------------------------------------------------------------------------


def test_example_toolset_discovered():
    toolsets = plugins.discover_toolsets()
    assert "example" in toolsets
    assert isinstance(toolsets["example"], Toolset)
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig()), agent=None)
    built = toolsets["example"].build(ctx)
    assert any(getattr(fn, "__name__", None) == "roll_dice" for fn in built)


def test_tool_pack_tools_reach_a_role_that_names_the_pack(tmp_path):
    """A pack's tools only ever reach the workers whose roles name it; the supervisor mounts none."""
    from kokua.core.build import _build_subagent_agent_types, _load_plugin_tools_by_pack

    cfg = _config(tmp_path, subagent_roles={"roller": {"description": "Rolls.", "tool_packs": ["example"]}})
    types = _build_subagent_agent_types(cfg, [], _load_plugin_tools_by_pack(cfg))
    assert "roll_dice" in {fn.__name__ for fn in types["roller"]["tools"]}


def test_a_role_that_names_no_pack_gets_no_pack_tools(tmp_path):
    from kokua.core.build import _build_subagent_agent_types, _load_plugin_tools_by_pack

    cfg = _config(tmp_path, subagent_roles={"plain": {"description": "Plain.", "groups": ["compute"]}})
    types = _build_subagent_agent_types(cfg, [], _load_plugin_tools_by_pack(cfg))
    assert "roll_dice" not in {fn.__name__ for fn in types["plain"]["tools"]}


async def test_no_plugins_flag_omits_tool_pack_tools(tmp_path):
    from kokua.core.build import _load_plugin_tools_by_pack

    assert _load_plugin_tools_by_pack(_config(tmp_path, load_plugins=False)) == {}
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
