"""The registry assembled from every provider, and one agent's tools resolved from it."""

import pytest

from kokua.config.file import ConfigError
from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.toolsets.agents import build_registry, unreferenced_toolsets, validate_agents
from kokua.toolsets.registry import ToolsetError


def test_registry_contains_every_provider():
    registry = build_registry(AssistantConfig())
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
        "aimu_agents",
        "github_backup",
        "image",
    ):
        assert name in registry, name


def test_kokuas_own_plugins_are_labeled_built_in_not_plugin():
    """The toolsets Kokua's own distribution registers under `kokua.toolsets` are told apart from a
    third party's by provenance (which distribution registered the entry point), not a hand-maintained
    name list, so unreferenced_toolsets can exempt them without knowing their names in advance."""
    registry = build_registry(AssistantConfig())
    for name in ("aimu_agents", "image"):
        assert registry.providers[name] == "built-in toolset", name


def test_a_third_party_plugin_is_still_labeled_plugin(monkeypatch):
    from kokua.toolsets import agents
    from kokua.toolsets.registry import Toolset

    weather = Toolset(name="weather", description="Forecasts.", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"weather": weather})
    registry = build_registry(AssistantConfig())
    assert registry.providers["weather"] == "plugin"


def test_a_plugin_shadowing_a_core_name_is_rejected(monkeypatch):
    from kokua.toolsets import agents
    from kokua.toolsets.registry import Toolset

    clash = Toolset(name="memory", description="clash", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"memory": clash})
    with pytest.raises(ToolsetError) as excinfo:
        build_registry(AssistantConfig())
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
        registry = build_registry(AssistantConfig())

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

    registry = build_registry(AssistantConfig())
    for source in (*BUILTIN_TOOLSETS, *CORE_TOOLSETS):
        assert registry[source.name].build is source.build, source.name


def _config(agents, entry="assistant") -> AssistantConfig:
    return AssistantConfig(agents=agents, entry_agent=entry)


def _valid() -> dict:
    return {
        "assistant": AgentConfig(tools=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }


def test_a_valid_graph_passes():
    config = _config(_valid())
    validate_agents(config, build_registry(config))


def test_no_agents_at_all_is_rejected():
    """Reachable only when config.toml already exists (config/file.py's own missing-file error fires
    first otherwise), so plain `kokua config init` would refuse to overwrite it -- the message must name
    a remedy that actually works from that state, not the one that requires no file to be there yet."""
    config = _config({})
    with pytest.raises(ConfigError, match="at least one") as excinfo:
        validate_agents(config, build_registry(config))
    message = str(excinfo.value)
    assert "config.example.toml" in message
    assert "kokua config init --force" in message


def test_a_missing_entry_agent_is_rejected():
    config = _config(_valid(), entry="supervisor")
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "supervisor" in str(excinfo.value)


def test_an_unknown_delegation_target_is_rejected():
    agents = _valid()
    agents["assistant"] = AgentConfig(tools=["time"], delegates_to=["reasercher"])
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "reasercher" in str(excinfo.value)


def test_a_delegation_cycle_is_rejected_and_named():
    agents = {
        "assistant": AgentConfig(delegates_to=["a"]),
        "a": AgentConfig(delegates_to=["b"]),
        "b": AgentConfig(delegates_to=["a"]),
    }
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    message = str(excinfo.value)
    assert "a" in message and "b" in message
    assert "cycle" in message.lower()


def test_an_agent_delegating_to_itself_is_rejected():
    config = _config({"assistant": AgentConfig(delegates_to=["assistant"])})
    with pytest.raises(ConfigError, match="cycle"):
        validate_agents(config, build_registry(config))


def test_an_unknown_toolset_name_is_rejected_during_validation():
    config = _config({"assistant": AgentConfig(tools=["memry"])})
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "memry" in str(excinfo.value)


def test_skills_on_a_worker_is_rejected_during_validation():
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["skills"]),
    }
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "skills" in str(excinfo.value)


def test_a_worker_model_may_carry_an_endpoint_override():
    """A worker's model accepts the same extended string ``[assistant].model`` does.

    The two are validated by different code (this one parses, ``[assistant].model`` builds a
    throwaway client), so they can drift apart, and a validator that understood only
    ``provider:model_id`` would refuse an endpoint the entry agent runs on happily: pinning a
    worker to the same host the assistant uses would fail at startup.
    """
    agents = {
        "assistant": AgentConfig(tools=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], model="ollama:qwen3.5:9b@http://example.local:11434"),
    }
    config = _config(agents)
    validate_agents(config, build_registry(config))


def test_an_unresolvable_worker_model_is_still_rejected():
    """The endpoint grammar widens what parses, not what passes: a bad id fails as before."""
    agents = {
        "assistant": AgentConfig(tools=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], model="ollama:no-such-model@http://example.local:11434"),
    }
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "researcher" in str(excinfo.value)


def test_unreferenced_toolsets_ignores_unnamed_builtin_and_core_groups():
    """A built-in AIMU group or a core subsystem toolset ships whether or not any agent names it, so an
    unnamed one is not a startup warning -- only a name the user provisioned earns one."""
    config = _config({"assistant": AgentConfig(tools=["time"])})
    registry = build_registry(config)
    assert unreferenced_toolsets(config, registry) == []


def test_unreferenced_toolsets_is_silent_on_the_real_shipped_config():
    """The regression this guards: Kokua's own built-in toolsets register under the real
    `kokua.toolsets` entry-point group, a path a monkeypatched `discover_toolsets` (used above) does not
    exercise, and the shipped config.example.toml declares only some of them. Without excluding Kokua's
    own distribution from the warning, every default install would log one warning per shipped toolset
    nobody chose to skip."""
    from tests.channels import example_agents

    config = AssistantConfig(agents=example_agents(), entry_agent="assistant")
    registry = build_registry(config)
    assert unreferenced_toolsets(config, registry) == []


def test_unreferenced_toolsets_reports_an_unused_plugin(monkeypatch):
    from kokua.toolsets import agents
    from kokua.toolsets.registry import Toolset

    unused = Toolset(name="weather", description="Forecasts.", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"weather": unused})
    config = AssistantConfig(agents={"assistant": AgentConfig(tools=["time"])})
    registry = build_registry(config)
    assert unreferenced_toolsets(config, registry) == ["weather"]


def test_unreferenced_toolsets_does_not_report_a_declared_plugin(monkeypatch):
    from kokua.toolsets import agents
    from kokua.toolsets.registry import Toolset

    used = Toolset(name="weather", description="Forecasts.", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"weather": used})
    config = AssistantConfig(agents={"assistant": AgentConfig(tools=["time", "weather"])})
    registry = build_registry(config)
    assert unreferenced_toolsets(config, registry) == []


def test_a_configured_section_for_an_undeclared_toolset_is_reported():
    from tests.channels import example_agents
    from kokua.toolsets.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].tools = ["time"]
    config = AssistantConfig(agents=agents, entry_agent="assistant", configured_sections=("planning",))

    assert configured_but_undeclared(config) == ["planning"]


def test_a_configured_section_for_a_declared_toolset_is_not_reported():
    from tests.channels import example_agents
    from kokua.toolsets.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].tools = ["planning"]
    config = AssistantConfig(agents=agents, entry_agent="assistant", configured_sections=("planning",))

    assert configured_but_undeclared(config) == []


def test_a_defaulted_section_the_file_never_had_is_not_reported():
    """`toolset_settings` carries a bucket for every declared setting once seeding has run, so a
    section that was only ever defaulted (never in configured_sections) must not be reported even
    though it appears here."""
    from tests.channels import example_agents
    from kokua.toolsets.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].tools = ["time"]
    config = AssistantConfig(
        agents=agents, entry_agent="assistant", toolset_settings={"planning": {"plan_review": False}}
    )

    assert configured_but_undeclared(config) == []


def test_a_diamond_shaped_graph_passes():
    """Two agents delegating to a shared target is not a cycle: the DFS must mark a finished node
    ``done`` so revisiting it through a second path does not re-walk it or read as a cycle."""
    agents = {
        "assistant": AgentConfig(delegates_to=["a", "b"]),
        "a": AgentConfig(delegates_to=["shared"]),
        "b": AgentConfig(delegates_to=["shared"]),
        "shared": AgentConfig(),
    }
    config = _config(agents)
    validate_agents(config, build_registry(config))
