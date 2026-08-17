"""Command dispatch: a workflow's command exists only where its toolset is declared."""

from __future__ import annotations

import pytest

from kokua.config import AssistantConfig
from kokua.config.file import ConfigError
from kokua.toolsets.agents import build_command_map
from kokua.toolsets.registry import Toolset, register
from kokua.workflows import Workflow
from tests.channels import example_agents


def _workflow(command: str) -> Workflow:
    return Workflow(name=command, description="W.", command=command, usage=f"/{command} <x>", build=lambda ctx: None)


def _registry(*toolsets: Toolset):
    return register([("test", list(toolsets))])


def test_a_declared_workflow_gets_its_command():
    agents = example_agents()
    agents["assistant"].tools = ["planning"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(Toolset(name="planning", description="P.", build=lambda ctx: [], workflow=_workflow("plan")))

    assert set(build_command_map(config, registry)) == {"plan"}


def test_an_undeclared_workflow_gets_no_command():
    agents = example_agents()
    agents["assistant"].tools = ["time"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(
        Toolset(name="time", description="T.", build=lambda ctx: []),
        Toolset(name="planning", description="P.", build=lambda ctx: [], workflow=_workflow("plan")),
    )

    assert build_command_map(config, registry) == {}


def test_a_workflow_may_not_claim_a_reserved_command():
    agents = example_agents()
    agents["assistant"].tools = ["stopper"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(Toolset(name="stopper", description="S.", build=lambda ctx: [], workflow=_workflow("stop")))

    with pytest.raises(ConfigError, match="reserved"):
        build_command_map(config, registry)


def test_two_workflows_may_not_claim_one_command():
    agents = example_agents()
    agents["assistant"].tools = ["a", "b"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(
        Toolset(name="a", description="A.", build=lambda ctx: [], workflow=_workflow("go")),
        Toolset(name="b", description="B.", build=lambda ctx: [], workflow=_workflow("go")),
    )

    with pytest.raises(ConfigError, match="both offer the /go command"):
        build_command_map(config, registry)
