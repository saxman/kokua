"""AIMU's web group, wrapped. The wrapper adds exactly one thing, and it is the guidance."""

from __future__ import annotations

from aimu.tools import builtin as aimu_builtin

from kokua.config.schema import AssistantConfig
from kokua.registry import LiveState, ToolsetContext
from kokua.toolsets.web import TOOLSET


def test_it_builds_exactly_aimus_tools(tmp_path):
    """The wrapper is a wrapper: it hands over AIMU's callables and adds none of its own. Asserted for
    one group rather than all eight, since they are generated from one shape."""
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=None, agent_name="assistant")

    assert TOOLSET.build(ctx) == list(aimu_builtin.web)


def test_it_is_the_one_aimu_group_carrying_guidance():
    """A tool schema says what `web_search` does, never when to prefer it over the model's own memory,
    which is the whole failure: a model that believes it knows the answer never reaches for the tool.
    `web` is the one AIMU group whose trigger is epistemic rather than obvious from its name, so it is
    the one that earns the prompt tokens; `fs` and `compute` are reached for when a task plainly needs
    them."""
    from kokua.toolsets import compute, fs

    assert "look it up" in TOOLSET.guidance
    assert fs.TOOLSET.guidance == ""
    assert compute.TOOLSET.guidance == ""
