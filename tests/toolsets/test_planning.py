"""The planning toolset: a capability that contributes a turn strategy instead of tools."""

from __future__ import annotations

from kokua.config.schema import AssistantConfig
from kokua.registry import LiveState, ToolsetContext
from kokua.toolsets.planning import TOOLSET


def test_it_carries_a_workflow_and_no_tools(tmp_path):
    """What `Toolset.workflow` exists for: `/plan` is granted by declaring "planning" in an agent's
    `tools`, exactly the way a tool is, and nothing about it reaches the model as a tool."""
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=None, agent_name="assistant")

    assert TOOLSET.workflow is not None
    assert TOOLSET.build(ctx) == []
