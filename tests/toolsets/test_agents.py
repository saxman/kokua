"""The registry assembled from every provider, and one agent's tools resolved from it."""

import pytest

from kokua.config.schema import AssistantConfig
from kokua.toolsets.agents import build_registry
from kokua.toolsets.registry import ToolsetError


def test_registry_contains_every_provider():
    registry = build_registry(AssistantConfig(load_plugins=True))
    for name in (
        "web",
        "time",
        "memory",
        "documents",
        "skills",
        "config",
        "mcp-admin",
        "scheduling",
        "conversations",
        "pdf",
        "email",
    ):
        assert name in registry, name


def test_plugins_are_absent_when_load_plugins_is_off():
    registry = build_registry(AssistantConfig(load_plugins=False))
    assert "web" in registry
    assert "pdf" not in registry


def test_a_plugin_shadowing_a_core_name_is_rejected(monkeypatch):
    from kokua.toolsets import agents
    from kokua.toolsets.registry import Toolset

    clash = Toolset(name="memory", description="clash", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"memory": clash})
    with pytest.raises(ToolsetError) as excinfo:
        build_registry(AssistantConfig(load_plugins=True))
    assert "memory" in str(excinfo.value)
    assert "plugin" in str(excinfo.value)
