"""Tests for the `aimu_agents` toolset: AIMU's prebuilt orchestrator agents mounted as tools.

The prebuilts each build one orchestrator client plus three worker clients and then talk to a model,
so every test here stubs both the client factory and the prebuilt classes. What is actually under test
is Kokua's wiring: that construction is deferred to call time, that a call gets a fresh agent, that the
global tool policy still governs the workers, and that a bad model does not raise into the agent loop.
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
    base = {"data_dir": tmp_path, "memory": False, "model": "anthropic:claude-sonnet-4-6"}
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


def test_pack_tools_reach_a_role_that_names_the_pack(tmp_path):
    """The documented wiring: a role with tool_packs = ["aimu_agents"] gets the three agents as tools."""
    from kokua.core.build import _build_subagent_agent_types

    cfg = _assistant_config(
        tmp_path, subagent_roles={"reviewer": {"description": "Reviews.", "tool_packs": ["aimu_agents"]}}
    )
    types = _build_subagent_agent_types(cfg)
    names = {fn.__name__ for fn in types["reviewer"]["tools"]}
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


def test_research_workers_get_web_tools_when_the_web_group_is_enabled(tmp_path, stub_prebuilts):
    tools = aimu_agents.build(_config(tmp_path, tools=["web", "compute"]))
    _tool(tools, "research_report")("photosynthesis")
    worker_tools = stub_prebuilts["instances"][0].kwargs["worker_tools"]
    assert {fn.__name__ for fn in worker_tools} >= {"web_search", "get_webpage"}


def test_research_workers_get_no_tools_when_the_web_group_is_disabled(tmp_path, stub_prebuilts):
    """A role never exceeds [tools].groups; a pack-mounted agent's workers must not either."""
    tools = aimu_agents.build(_config(tmp_path, tools=["none"]))
    _tool(tools, "research_report")("photosynthesis")
    assert stub_prebuilts["instances"][0].kwargs["worker_tools"] is None


def test_tools_all_also_enables_the_research_workers_web_tools(tmp_path, stub_prebuilts):
    tools = aimu_agents.build(_config(tmp_path, tools=["all"]))
    _tool(tools, "research_report")("photosynthesis")
    assert stub_prebuilts["instances"][0].kwargs["worker_tools"]


def test_an_unresolvable_model_returns_a_message_instead_of_raising(tmp_path, monkeypatch):
    """A tool that raises breaks the agent's tool loop, so the failure has to come back as a result."""

    def fake_client(model, **kwargs):
        raise ValueError("no model configured")

    monkeypatch.setattr(aimu, "client", fake_client)
    result = _tool(aimu_agents.build(_config(tmp_path, model=None)), "code_review")("def f(): pass")
    assert "no model configured" in result
