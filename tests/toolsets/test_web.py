"""AIMU's web group plus its page tools, wrapped, with guidance on when to use which."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from aimu.tools import builtin as aimu_builtin

from kokua.config.schema import AssistantConfig
from kokua.registry import LiveState, ToolsetContext
from kokua.toolsets.web import TOOLSET


def _context(data_dir):
    return ToolsetContext(
        state=LiveState(config=AssistantConfig(data_dir=data_dir)), agent=None, agent_name="assistant"
    )


def test_it_builds_aimus_web_group_and_then_its_page_tools(tmp_path):
    """AIMU's own callables, unchanged and first, so `get_web_content` is still there to read a page
    whole; then the three page tools, which add only the passages a question needs."""
    tools = TOOLSET.build(_context(tmp_path))

    assert tools[: len(aimu_builtin.web)] == list(aimu_builtin.web)
    assert [fn.__name__ for fn in tools[len(aimu_builtin.web) :]] == ["fetch_page", "read_page", "find_in_page"]


def test_pages_live_under_the_data_dir_and_every_agent_reads_the_same_ones(tmp_path):
    """A worker's handle has to work for the agent it reports to, so the store is the directory under
    `$KOKUA_HOME/data`, not one per `build`. Each build gets its own tool closures over that one place."""
    config = AssistantConfig(data_dir=tmp_path)
    assert config.pages_path == tmp_path / "pages"

    fetch_page = {fn.__name__: fn for fn in TOOLSET.build(_context(tmp_path))}["fetch_page"]
    read_page = {fn.__name__: fn for fn in TOOLSET.build(_context(tmp_path))}["read_page"]
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "text/plain"}
    response.encoding = "utf-8"
    response.iter_content = lambda chunk_size=1: iter([b"# Notes\n\nThe answer is forty-two."])
    with patch("aimu.tools.builtin.requests.request", return_value=response):
        fetched = fetch_page("https://example.com", question="answer")
    handle = re.search(r"page-[0-9a-f]{12}", fetched).group(0)

    assert (config.pages_path / f"{handle}.md").exists()
    assert "forty-two" in read_page(handle)


def test_guidance_says_which_reader_to_reach_for():
    """Two tools read a page, and the schemas alone do not say when each one is the right one: the
    tradeoff (ranked passages that can miss an answer phrased differently, against the whole page at
    its full cost in context) has to be in the prompt."""
    assert "fetch_page" in TOOLSET.guidance and "get_web_content" in TOOLSET.guidance
    assert "find_in_page" in TOOLSET.guidance


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
