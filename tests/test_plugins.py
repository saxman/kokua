"""Tests for the entry-point plugin system (front ends + toolsets)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import MockAsyncModelClient
from kokua import plugins
from kokua.core.assistant import Assistant
from kokua.config import AssistantConfig
from tests.channels import example_agents
from kokua.plugins import FrontEnd, Toolset
from kokua.toolsets import LiveState, ToolsetContext


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "agents": example_agents(), "entry_agent": "assistant"}
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


def _worker_specs(cfg) -> dict[str, dict]:
    from kokua.toolsets.agents import build_agent_specs, build_registry

    state = LiveState(config=cfg, registry=build_registry(cfg))
    return build_agent_specs(cfg, state, cfg.entry_agent)


def _two_workers(tmp_path):
    """One worker declaring the `example` plugin toolset and one declaring only a built-in group."""
    from kokua.config.schema import AgentConfig

    return _config(
        tmp_path,
        agents={
            "assistant": AgentConfig(tools=["time"], delegates_to=["roller", "plain"]),
            "roller": AgentConfig(description="Rolls.", tools=["example"]),
            "plain": AgentConfig(description="Plain.", tools=["compute"]),
        },
    )


def test_plugin_tools_reach_an_agent_that_names_the_toolset(tmp_path):
    """A plugin's tools reach an agent by one route only: that agent naming the toolset in `tools`."""
    specs = _worker_specs(_two_workers(tmp_path))
    assert "roll_dice" in {fn.__name__ for fn in specs["roller"]["tools"]}


def test_an_agent_that_names_no_plugin_gets_no_plugin_tools(tmp_path):
    specs = _worker_specs(_two_workers(tmp_path))
    assert "roll_dice" not in {fn.__name__ for fn in specs["plain"]["tools"]}


async def test_no_plugins_flag_omits_plugin_toolsets(tmp_path):
    from kokua.toolsets.agents import build_registry

    assert "example" not in build_registry(_config(tmp_path, load_plugins=False))
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
