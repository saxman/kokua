"""AIMU's groups and stores, wrapped as toolsets. The identities and the cross_cutting flags matter:
the flags are what the delegation guidance reads to tell a lean agent from a tool-heavy one."""

from aimu.tools import builtin as aimu_builtin

from kokua.config.schema import AssistantConfig
from kokua.toolsets.builtin import BUILTIN_TOOLSETS
from kokua.toolsets.context import LiveState, ToolsetContext

BY_NAME = {t.name: t for t in BUILTIN_TOOLSETS}


def _ctx(tmp_path, agent=None) -> ToolsetContext:
    return ToolsetContext(state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=agent)


def test_every_aimu_group_is_registered():
    for name in ("web", "fs", "compute", "time", "misc", "image", "audio", "speech", "transcription"):
        assert name in BY_NAME


def test_a_group_builds_exactly_aimus_tools(tmp_path):
    assert BY_NAME["web"].build(_ctx(tmp_path)) == list(aimu_builtin.web)


def test_time_is_cross_cutting_but_fs_is_not():
    assert BY_NAME["time"].cross_cutting is True
    assert BY_NAME["fs"].cross_cutting is False


def test_memory_and_documents_are_separate_cross_cutting_toolsets():
    assert BY_NAME["memory"].cross_cutting is True
    assert BY_NAME["documents"].cross_cutting is True
    assert BY_NAME["memory"].guidance != BY_NAME["documents"].guidance


def test_memory_builds_tools_over_the_shared_store(tmp_path):
    ctx = _ctx(tmp_path)
    names = {fn.__name__ for fn in BY_NAME["memory"].build(ctx)}
    assert "store_memory" in names
    assert "search_memories" in names


def test_documents_builds_the_document_tools(tmp_path):
    names = {fn.__name__ for fn in BY_NAME["documents"].build(_ctx(tmp_path))}
    assert "save_document" in names
    assert "search_documents" in names


def test_skills_is_entry_point_only():
    assert BY_NAME["skills"].entry_point_only is True
    assert BY_NAME["skills"].cross_cutting is True


def test_skills_builds_the_authoring_and_script_tools(tmp_path):
    class FakeAgent:
        tools: list = []

    names = {fn.__name__ for fn in BY_NAME["skills"].build(_ctx(tmp_path, agent=FakeAgent()))}
    assert names == {"author_skill", "add_skill_script"}
