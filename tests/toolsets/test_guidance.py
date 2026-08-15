"""An agent's prompt is assembled from what it declares, so no sentence in it can go stale."""

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.toolsets.agents import assemble_system_message, build_registry
from kokua.toolsets.registry import Toolset, select


def _assemble(agents, name="assistant"):
    config = AssistantConfig(agents=agents, entry_agent="assistant", load_plugins=False)
    registry = build_registry(config)
    toolsets = select(agents[name].tools, registry, agent=name, entry_point="assistant")
    return assemble_system_message(config, name, toolsets)


def test_the_declared_message_comes_first():
    agents = {"assistant": AgentConfig(system_message="Custom opener.", tools=["memory"])}
    assert _assemble(agents).startswith("Custom opener.")


def test_a_toolsets_guidance_is_appended():
    agents = {"assistant": AgentConfig(system_message="Opener.", tools=["memory"])}
    assert "store_memory" in _assemble(agents)


def test_guidance_follows_declared_order():
    agents = {"assistant": AgentConfig(system_message="Opener.", tools=["documents", "memory"])}
    message = _assemble(agents)
    assert message.index("save_document") < message.index("store_memory")


def test_a_toolset_declared_twice_contributes_guidance_once():
    agents = {"assistant": AgentConfig(system_message="Opener.", tools=["memory", "memory"])}
    assert _assemble(agents).count("store_memory") == 1


def test_an_undeclared_toolsets_guidance_is_absent():
    agents = {"assistant": AgentConfig(system_message="Opener.", tools=["time"])}
    assert "store_memory" not in _assemble(agents)


def test_a_non_delegating_agent_gets_no_delegation_guidance():
    agents = {"assistant": AgentConfig(system_message="Opener.", tools=["time"])}
    assert "spawn_subagent" not in _assemble(agents)


def test_a_delegating_agent_gets_the_mechanism():
    agents = {
        "assistant": AgentConfig(system_message="Opener.", tools=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    assert "spawn_subagent" in _assemble(agents)


def test_a_lean_delegator_is_told_it_has_almost_no_direct_tools():
    agents = {
        "assistant": AgentConfig(system_message="Opener.", tools=["time", "memory"], delegates_to=["r"]),
        "r": AgentConfig(tools=["web"]),
    }
    assert "almost no direct tools" in _assemble(agents)


def test_a_delegator_holding_a_domain_toolset_is_not():
    agents = {
        "assistant": AgentConfig(system_message="Opener.", tools=["time", "fs"], delegates_to=["r"]),
        "r": AgentConfig(tools=["web"]),
    }
    message = _assemble(agents)
    assert "spawn_subagent" in message
    assert "almost no direct tools" not in message


def test_the_default_opener_is_used_when_an_agent_declares_none():
    agents = {"assistant": AgentConfig(tools=["time"])}
    assert "personal assistant" in _assemble(agents)


def test_a_third_party_toolsets_guidance_reaches_the_prompt(monkeypatch):
    from kokua.toolsets import agents as agents_module

    weather = Toolset(
        name="weather",
        description="Forecasts.",
        build=lambda ctx: [],
        guidance=" Call `forecast` for weather.",
    )
    monkeypatch.setattr(agents_module, "discover_toolsets", lambda: {"weather": weather})
    config = AssistantConfig(
        agents={"assistant": AgentConfig(system_message="Opener.", tools=["weather"])},
        entry_agent="assistant",
        load_plugins=True,
    )
    registry = build_registry(config)
    toolsets = select(["weather"], registry, agent="assistant", entry_point="assistant")
    assert "forecast" in assemble_system_message(config, "assistant", toolsets)
