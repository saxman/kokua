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


def test_a_spec_carries_the_model_its_agent_declares(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], model="ollama:qwen3:32b"),
    }
    config, state = _state(tmp_path, agents)
    assert build_agent_specs(config, state, "assistant")["researcher"]["model"] == "ollama:qwen3:32b"


def test_a_spec_declaring_no_model_carries_none_so_the_spawn_default_applies(tmp_path):
    """AIMU reads a missing spec ``model`` as "use the tool's own model", which is the default one the
    delegate was built with. A worker therefore inherits the default rather than its delegator's pin."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"], model="ollama:qwen3:32b"),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    assert "model" not in build_agent_specs(config, state, "assistant")["researcher"]


class _FakeClient:
    def __init__(self, model):
        self.model = model


class _FakeAgent:
    """Enough of a live agent for ``make_delegation_tool``: a name and a client carrying a model."""

    def __init__(self, name, model):
        self.name = name
        self.model_client = _FakeClient(model)


def _captured_spawn_model(monkeypatch, config, state, agent) -> object:
    from kokua.toolsets import agents as agents_mod

    captured = []

    def fake_make(model, **kwargs):
        captured.append(model)

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """menu"""
            return "ok"

        spawn_subagent.__name__ = "spawn_subagent"
        return spawn_subagent

    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", fake_make)
    agents_mod.make_delegation_tool(agent, config, state)
    return captured[0]


def test_the_delegate_is_built_with_the_default_model_not_the_delegators_pin(tmp_path, monkeypatch):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"], model="ollama:qwen3:32b"),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    config.model = "ollama:qwen3:8b"
    agent = _FakeAgent("assistant", "ollama:qwen3:32b")
    assert _captured_spawn_model(monkeypatch, config, state, agent) == "ollama:qwen3:8b"


def test_an_unset_default_falls_back_to_the_model_the_delegator_already_resolved(tmp_path, monkeypatch):
    """With no [assistant].model, AIMU resolved one when the entry agent's client was built. Reusing
    that string keeps every spawn on the same model instead of re-resolving per spawn."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    agent = _FakeAgent("assistant", "ollama:auto-resolved")
    assert _captured_spawn_model(monkeypatch, config, state, agent) == "ollama:auto-resolved"
