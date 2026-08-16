"""Kokua owns the delegation topology: each agent's menu is its own targets, and a target that
delegates carries its own delegate rather than inheriting the parent's menu."""

from aimu.tools import builtin

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.toolsets.agents import build_agent_specs, build_registry
from kokua.toolsets.context import LiveState

SPAWN = "spawn_subagent"


def _state(tmp_path, agents, entry="assistant") -> tuple[AssistantConfig, LiveState]:
    config = AssistantConfig(data_dir=tmp_path, agents=agents, entry_agent=entry, load_plugins=False)
    state = LiveState(config=config, registry=build_registry(config))
    return config, state


def test_a_spec_is_produced_for_each_target(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher", "coder"]),
        "researcher": AgentConfig(tools=["web"]),
        "coder": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    specs = build_agent_specs(config, state, "assistant")
    assert sorted(specs) == ["coder", "researcher"]


def test_a_spec_carries_only_its_own_declared_tools(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["researcher"]["tools"]}
    assert names == {fn.__name__ for fn in builtin.web}
    assert SPAWN not in names


def test_a_spec_description_opens_its_system_message(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(description="Research specialist.", tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    spec = build_agent_specs(config, state, "assistant")["researcher"]
    assert spec["system_message"].startswith("Research specialist.")


def test_a_target_that_delegates_gets_its_own_delegate(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["lead"]),
        "lead": AgentConfig(tools=["time"], delegates_to=["helper"]),
        "helper": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["lead"]["tools"]}
    assert SPAWN in names


def test_a_leaf_target_gets_no_delegate(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["helper"]),
        "helper": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["helper"]["tools"]}
    assert SPAWN not in names


def test_an_agent_with_no_targets_produces_no_specs(tmp_path):
    config, state = _state(tmp_path, {"assistant": AgentConfig(tools=["time"])})
    assert build_agent_specs(config, state, "assistant") == {}


def test_a_nested_spawn_tool_offers_only_the_childs_own_targets(tmp_path):
    """The whole point of Kokua owning the topology: a nested spawn tool's menu is the child's own
    delegates_to, never the parent's menu it was built under."""
    agents = {
        "assistant": AgentConfig(delegates_to=["lead"]),
        "lead": AgentConfig(tools=["time"], delegates_to=["helper", "other"]),
        "helper": AgentConfig(tools=["fs"]),
        "other": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    lead_tools = build_agent_specs(config, state, "assistant")["lead"]["tools"]
    spawn = next(fn for fn in lead_tools if fn.__name__ == SPAWN)
    assert "helper" in spawn.__doc__
    assert "other" in spawn.__doc__
    assert "lead" not in spawn.__doc__
