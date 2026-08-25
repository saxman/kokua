"""Tests for the entry-point plugin system (front ends + toolsets)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kokua import plugins
from kokua.config import AssistantConfig
from tests.channels import example_agents
from kokua.plugins import FrontEnd, Toolset
from kokua.registry import LiveState, ToolsetContext


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


def test_a_built_in_toolset_is_discovered():
    toolsets = plugins.discover_toolsets()
    assert "aimu_agents" in toolsets
    assert isinstance(toolsets["aimu_agents"], Toolset)
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig()), agent=None, agent_name="assistant")
    built = toolsets["aimu_agents"].build(ctx)
    assert any(getattr(fn, "__name__", None) == "code_review" for fn in built)


def _worker_specs(cfg) -> dict[str, dict]:
    from kokua.core.agents import build_agent_specs, build_registry

    state = LiveState(config=cfg, registry=build_registry(cfg))
    return build_agent_specs(cfg, state, cfg.entry_agent)


def _two_workers(tmp_path):
    """One worker declaring a plugin toolset and one declaring only a built-in group."""
    from kokua.config.schema import AgentConfig

    return _config(
        tmp_path,
        agents={
            "assistant": AgentConfig(tools=["time"], delegates_to=["roller", "plain"]),
            "roller": AgentConfig(description="Reviews.", tools=["aimu_agents"]),
            "plain": AgentConfig(description="Plain.", tools=["compute"]),
        },
    )


def test_plugin_tools_reach_an_agent_that_names_the_toolset(tmp_path):
    """A plugin's tools reach an agent by one route only: that agent naming the toolset in `tools`."""
    specs = _worker_specs(_two_workers(tmp_path))
    assert "code_review" in {fn.__name__ for fn in specs["roller"]["tools"]}


def test_an_agent_that_names_no_plugin_gets_no_plugin_tools(tmp_path):
    specs = _worker_specs(_two_workers(tmp_path))
    assert "code_review" not in {fn.__name__ for fn in specs["plain"]["tools"]}


def test_entry_point_toolsets_are_registered_unconditionally(tmp_path):
    """There is no switch. Installing a distribution that registers a `kokua.toolsets` entry point is
    the consent, so every discovered toolset is in the namespace. What an agent may *use* is unchanged:
    exactly what its own `tools` list declares, which is what the sibling test above pins."""
    from kokua.core.agents import build_registry

    assert "aimu_agents" in build_registry(_config(tmp_path))


def test_an_entry_point_that_fails_to_import_is_not_swallowed(monkeypatch):
    """Discovery used to catch every exception from `ep.load()` and continue with no log line, so a
    module that raised on import vanished and the user was told their toolset name was unknown, with
    the actual ImportError recorded nowhere. Kokua ships no third-party code, so it carries no special
    handling for code that cannot load: the failure names the module."""

    class _BoomEntryPoint:
        name = "boom"
        dist = None

        def load(self):
            raise ImportError("no module named nope")

    monkeypatch.setattr(plugins, "entry_points", lambda group: [_BoomEntryPoint()])

    with pytest.raises(ImportError, match="no module named nope"):
        plugins.discover_toolsets()
