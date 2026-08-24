"""Tests for the `aimu_agents` toolset: AIMU's prebuilt orchestrator agents mounted as tools.

The prebuilts each build one orchestrator client plus three worker clients and then talk to a model,
so every test here stubs both the client factory and the prebuilt classes. What is actually under test
is Kokua's wiring: that construction is deferred to call time, that a call gets a fresh agent, that the
research worker unconditionally receives web tools, and that a bad model does not raise into the agent
loop.
"""

from __future__ import annotations

from pathlib import Path

import aimu
import pytest

from tests.channels import _config as _assistant_config
from kokua import plugins
from kokua.config import AssistantConfig
from kokua.plugins import Toolset
from kokua.toolsets import LiveState, ToolsetContext, aimu_agents


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "model": "anthropic:claude-sonnet-4-6"}
    base.update(overrides)
    return AssistantConfig(**base)


class _RecordingPrebuilt:
    """Stands in for an AIMU prebuilt orchestrator, recording construction and run arguments."""

    def __init__(self, model_client, **kwargs):
        self.model_client = model_client
        self.kwargs = kwargs
        _RecordingPrebuilt.instances.append(self)

    def run(self, task):
        return f"ran: {task}"


@pytest.fixture
def stub_prebuilts(monkeypatch):
    """Replace the three prebuilts and the model-client factory; yield the list of built instances."""
    _RecordingPrebuilt.instances = []
    models: list = []

    def fake_client(model, **kwargs):
        models.append(model)
        return f"client-for-{model}"

    monkeypatch.setattr(aimu, "client", fake_client)
    for name in ("CodeReviewAgent", "ContentCreationAgent", "ResearchReportAgent"):
        monkeypatch.setattr(aimu_agents, name, _RecordingPrebuilt)
    return {"instances": _RecordingPrebuilt.instances, "models": models}


def _tool(tools: list, name: str):
    return next(fn for fn in tools if fn.__name__ == name)


def test_pack_is_discovered_and_contributes_three_tools():
    toolsets = plugins.discover_toolsets()
    assert "aimu_agents" in toolsets
    assert isinstance(toolsets["aimu_agents"], Toolset)
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig()), agent=None)
    names = {getattr(fn, "__name__", None) for fn in toolsets["aimu_agents"].build(ctx)}
    assert {"code_review", "research_report", "create_content"} <= names


def test_toolset_tools_reach_an_agent_that_names_the_toolset(tmp_path):
    """The documented wiring: an agent with tools = ["aimu_agents"] gets the three agents as tools."""
    from kokua.config.schema import AgentConfig
    from kokua.toolsets.agents import build_agent_specs, build_registry

    cfg = _assistant_config(
        tmp_path,
        agents={
            "assistant": AgentConfig(tools=["time"], delegates_to=["reviewer"]),
            "reviewer": AgentConfig(description="Reviews.", tools=["aimu_agents"]),
        },
    )
    state = LiveState(config=cfg, registry=build_registry(cfg))
    names = {fn.__name__ for fn in build_agent_specs(cfg, state, "assistant")["reviewer"]["tools"]}
    assert {"code_review", "research_report", "create_content"} <= names


def test_no_agent_is_constructed_until_a_tool_is_called(tmp_path, stub_prebuilts):
    """build() runs once per conversation agent, and each prebuilt makes four model clients, so eager
    construction would cost twelve clients per conversation and could load local model weights."""
    tools = aimu_agents.build(_config(tmp_path))
    assert stub_prebuilts["instances"] == []

    _tool(tools, "code_review")("def f(): pass")
    assert len(stub_prebuilts["instances"]) == 1


def test_each_call_builds_a_fresh_agent(tmp_path, stub_prebuilts):
    """A cached orchestrator's ModelClient.messages is shared mutable state, so two concurrent calls
    would interleave into one history."""
    code_review = _tool(aimu_agents.build(_config(tmp_path)), "code_review")
    code_review("first")
    code_review("second")
    assert len(stub_prebuilts["instances"]) == 2


def test_the_model_is_read_at_call_time(tmp_path, stub_prebuilts):
    """switch_model mutates the same config object the pack holds, so a runtime model switch has to
    reach these tools without rebuilding the pack."""
    config = _config(tmp_path)
    code_review = _tool(aimu_agents.build(config), "code_review")

    config.model = "anthropic:claude-opus-4-1"
    code_review("x")
    assert stub_prebuilts["models"] == ["anthropic:claude-opus-4-1"]


def test_a_prebuilt_runs_on_the_resolved_default_when_none_is_declared(tmp_path, stub_prebuilts):
    """With no [assistant].model this handed ``aimu.client`` None and let it resolve its own default.

    It reached the right endpoint by luck rather than by design, and it was a second resolution: the
    prebuilt could land on a different model than every Kokua agent, since nothing tied the two
    answers together. It now runs on the one string the config resolved.
    """
    from tests.conftest import TEST_DEFAULT_MODEL

    config = _config(tmp_path)
    config.model = None
    _tool(aimu_agents.build(config), "code_review")("x")
    assert stub_prebuilts["models"] == [TEST_DEFAULT_MODEL]


def test_research_workers_always_get_web_tools(tmp_path, stub_prebuilts):
    """There is no global tool policy left to gate this on: naming ``aimu_agents`` in an agent's
    ``tools`` is itself the consent, so building the toolset must not raise and the research worker
    must receive the web tools unconditionally."""
    tools = aimu_agents.build(_config(tmp_path))
    _tool(tools, "research_report")("photosynthesis")
    worker_tools = stub_prebuilts["instances"][0].kwargs["worker_tools"]
    assert {fn.__name__ for fn in worker_tools} >= {"web_search", "get_webpage"}


def test_an_unresolvable_model_returns_a_message_instead_of_raising(tmp_path, monkeypatch):
    """A tool that raises breaks the agent's tool loop, so the failure has to come back as a result."""

    def fake_client(model, **kwargs):
        raise ValueError("no model configured")

    monkeypatch.setattr(aimu, "client", fake_client)
    result = _tool(aimu_agents.build(_config(tmp_path, model=None)), "code_review")("def f(): pass")
    assert "no model configured" in result
