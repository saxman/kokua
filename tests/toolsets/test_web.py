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


def test_guidance_goes_to_the_aimu_groups_whose_trigger_is_epistemic():
    """A tool schema says what `web_search` does, never when to prefer it over the model's own memory,
    which is the whole failure: a model that believes it knows the answer never reaches for the tool.
    `compute` and `time` share it (a model believes it knows the figure, or the date), so they earn the
    prompt tokens too; `fs` does not, since a model asked about a file has nothing to answer from but
    the file."""
    from kokua.toolsets import compute, fs, time

    assert "look it up" in TOOLSET.guidance
    assert "rather than in prose" in compute.TOOLSET.guidance
    assert "`get_current_date_and_time`" in time.TOOLSET.guidance
    assert fs.TOOLSET.guidance == ""
