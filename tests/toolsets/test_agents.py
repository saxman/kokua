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


def test_a_plugin_that_fails_to_build_is_skipped_not_fatal(monkeypatch, caplog):
    """Third-party code is the only thing this registry is tolerant of: a broken plugin logs a warning
    and contributes no tools, rather than taking the whole assistant down."""
    import logging

    from kokua.toolsets import agents
    from kokua.toolsets.context import LiveState, ToolsetContext
    from kokua.toolsets.registry import Toolset

    def _boom(ctx):
        raise RuntimeError("boom")

    broken = Toolset(name="broken", description="broken", build=_boom)
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"broken": broken})

    with caplog.at_level(logging.WARNING, logger="kokua.toolsets.agents"):
        registry = build_registry(AssistantConfig(load_plugins=True))

    assert "broken" in registry  # registered by name; only *building* it is tolerant
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig()), agent=None)
    assert registry["broken"].build(ctx) == []
    assert any("broken" in record.getMessage() for record in caplog.records)


def test_core_and_builtin_toolsets_are_not_wrapped_with_tolerance():
    """The asymmetry is the point: only plugin-sourced toolsets are wrapped in the try/except that
    swallows a build failure. A core or AIMU toolset keeps its own ``build`` unchanged in the registry,
    so a bug there raises loudly instead of silently degrading to no tools."""
    from kokua.toolsets.builtin import BUILTIN_TOOLSETS
    from kokua.toolsets.core import CORE_TOOLSETS

    registry = build_registry(AssistantConfig(load_plugins=True))
    for source in (*BUILTIN_TOOLSETS, *CORE_TOOLSETS):
        assert registry[source.name].build is source.build, source.name
