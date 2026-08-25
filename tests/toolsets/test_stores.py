"""The three toolsets over AIMU's persistent state: memory, documents, and skill authoring.

Together rather than one file each because what matters about them is shared and comparative: each binds
to a lazy singleton on ``LiveState`` rather than building state of its own, which is what makes two
agents declaring one share a store over one directory instead of opening two.
"""

from __future__ import annotations

from kokua.config.schema import AssistantConfig
from kokua.registry import LiveState, ToolsetContext
from kokua.toolsets.documents import TOOLSET as DOCUMENTS
from kokua.toolsets.memory import TOOLSET as MEMORY
from kokua.toolsets.skills import TOOLSET as SKILLS


def _ctx(tmp_path, agent=None) -> ToolsetContext:
    return ToolsetContext(
        state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=agent, agent_name="assistant"
    )


def test_memory_builds_tools_over_the_shared_store(tmp_path):
    names = {fn.__name__ for fn in MEMORY.build(_ctx(tmp_path))}

    assert "store_memory" in names
    assert "search_memories" in names


def test_documents_builds_the_document_tools(tmp_path):
    names = {fn.__name__ for fn in DOCUMENTS.build(_ctx(tmp_path))}

    assert "save_document" in names
    assert "search_documents" in names


def test_memory_and_documents_are_separate_toolsets_with_their_own_guidance():
    """Split so an agent can hold one without the other, and so the prompt it carries names only the
    tools it actually has."""
    assert MEMORY.guidance != DOCUMENTS.guidance


def test_skills_builds_the_authoring_and_script_tools(tmp_path):
    """Only the two authoring tools. The catalogue, `activate_skill`, and one tool per skill script come
    from AIMU's `SkillAgent` regardless of whether this toolset is declared."""

    class FakeAgent:
        tools: list = []

    names = {fn.__name__ for fn in SKILLS.build(_ctx(tmp_path, agent=FakeAgent()))}

    assert names == {"author_skill", "add_skill_script"}


def test_skills_builds_against_a_none_agent_without_complaining(tmp_path):
    """Why `skills` is `entry_point_only`, and why the flag has to be checked at *declaration* rather
    than left to fail on its own. `make_skill_script_tool` uses its agent at CALL time
    (`await agent.reload_skills()`), not build time, so building against `ctx.agent = None` -- what every
    spawned worker gets -- succeeds quietly here and would fail mid-call, after the script was already
    written to disk. `select` refusing the declaration at startup is the only place that can catch it.
    """
    names = {fn.__name__ for fn in SKILLS.build(_ctx(tmp_path, agent=None))}

    assert names == {"author_skill", "add_skill_script"}
