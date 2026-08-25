"""The registry assembled from every provider, and one agent's tools resolved from it."""

import pytest

from kokua.config.file import ConfigError
from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.core.agents import build_registry, validate_agents
from kokua.registry.registry import ToolsetError


def test_registry_contains_every_provider():
    registry = build_registry(AssistantConfig())
    for name in (
        "web",
        "time",
        "memory",
        "documents",
        "skills",
        "config",
        "mcp",
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
    from kokua.core import agents
    from kokua.registry.registry import Toolset

    weather = Toolset(name="weather", description="Forecasts.", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"weather": weather})
    registry = build_registry(AssistantConfig())
    assert registry.providers["weather"] == "plugin"


def test_a_third_party_shadowing_a_shipped_name_is_rejected(monkeypatch):
    """Every toolset arrives through one entry-point group now, so a third party's package registering a
    name Kokua ships is a collision inside that group. The two are still told apart by which
    distribution registered them, which is what lets the message name each side, and it is the only
    thing that split is for."""
    from kokua.core import agents
    from kokua.registry.registry import Toolset

    shipped = Toolset(name="memory", description="Kokua's own.", build=lambda ctx: [])
    # Registered under a different entry-point key, which is not what collides: `register` keys on
    # TOOLSET.name, so this is a clash even though nothing in either table is spelled the same.
    theirs = Toolset(name="memory", description="A third party's.", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"memory": shipped, "their-key": theirs})
    monkeypatch.setattr(agents, "own_distribution_toolset_names", lambda: {"memory"})

    with pytest.raises(ToolsetError) as excinfo:
        build_registry(AssistantConfig())

    message = str(excinfo.value)
    assert "memory" in message
    assert "built-in toolset" in message
    assert "plugin" in message


def test_a_toolset_that_fails_to_build_raises(monkeypatch):
    """No source is tolerated, because there is no source this codebase is willing to be quiet about.
    A build failure is a bug in whoever wrote the toolset, and a warning in a log file is not how a
    person finds out an agent silently lost a capability."""
    from kokua.core import agents
    from kokua.registry.context import LiveState, ToolsetContext
    from kokua.registry.registry import Toolset

    def _boom(ctx):
        raise RuntimeError("boom")

    broken = Toolset(name="broken", description="broken", build=_boom)
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"broken": broken})

    registry = build_registry(AssistantConfig())
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig()), agent=None)
    with pytest.raises(RuntimeError, match="boom"):
        registry["broken"].build(ctx)


def test_no_toolset_is_wrapped_in_the_registry():
    """Every toolset in the registry carries the ``build`` its author wrote, whatever route it arrived
    by. The registry used to substitute a swallowing wrapper for entry-point toolsets, which meant a bug
    in one of Kokua's own became a log line while the same bug elsewhere took startup down."""
    from kokua.plugins import discover_toolsets

    registry = build_registry(AssistantConfig())
    for source in discover_toolsets().values():
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


def test_a_configured_section_for_an_undeclared_toolset_is_reported():
    from tests.channels import example_agents
    from kokua.core.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].tools = ["time"]
    config = AssistantConfig(agents=agents, entry_agent="assistant", configured_sections=("planning",))

    assert configured_but_undeclared(config) == ["planning"]


def test_a_configured_section_for_a_declared_toolset_is_not_reported():
    from tests.channels import example_agents
    from kokua.core.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].tools = ["planning"]
    config = AssistantConfig(agents=agents, entry_agent="assistant", configured_sections=("planning",))

    assert configured_but_undeclared(config) == []


def test_a_defaulted_section_the_file_never_had_is_not_reported():
    """`toolset_settings` carries a bucket for every declared setting once seeding has run, so a
    section that was only ever defaulted (never in configured_sections) must not be reported even
    though it appears here."""
    from tests.channels import example_agents
    from kokua.core.agents import configured_but_undeclared

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
