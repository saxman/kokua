"""AIMU's groups and stores, wrapped as toolsets. The identities and the cross_cutting flags matter:
the flags are what the delegation guidance reads to tell a lean agent from a tool-heavy one."""

from aimu.tools import builtin as aimu_builtin

from kokua.config.schema import AssistantConfig
from kokua.toolsets.builtin import BUILTIN_TOOLSETS
from kokua.registry.context import LiveState, ToolsetContext

BY_NAME = {t.name: t for t in BUILTIN_TOOLSETS}


def _ctx(tmp_path, agent=None) -> ToolsetContext:
    return ToolsetContext(state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=agent)


def test_every_aimu_group_is_registered():
    """AIMU's "image" group is deliberately excluded (see the omission comment in `builtin._GROUPS`)."""
    for name in ("web", "fs", "compute", "time", "misc", "audio", "speech", "transcription"):
        assert name in BY_NAME


def test_the_registered_image_toolset_is_not_aimus(tmp_path):
    """Kokua's `image` toolset (a plugin, saving into the servable images_path) supersedes AIMU's own
    "image" group (which saves where the web front end cannot serve it): both contribute a tool named
    `generate_image`, so registering both would let a name collision silently decide which an agent
    gets. Pinned here so a future change cannot silently re-register AIMU's group and reintroduce that.
    """
    assert "image" not in BY_NAME
    aimu_image_names = {fn.__name__ for fn in aimu_builtin.image}
    for toolset in BUILTIN_TOOLSETS:
        if toolset.entry_point_only:
            continue  # skills needs a real agent to build; irrelevant to the image collision anyway
        built_names = {fn.__name__ for fn in toolset.build(_ctx(tmp_path))}
        assert aimu_image_names.isdisjoint(built_names), toolset.name


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


def test_web_carries_guidance_but_the_other_groups_do_not():
    """A tool schema says what `web_search` does, never when to prefer it over the model's own memory,
    which is the whole failure: a model that believes it knows the answer never reaches for the tool.
    `web` is the one AIMU group whose trigger is epistemic rather than obvious from its name, so it is
    the one that earns guidance; `fs` and `compute` are reached for when the task plainly needs them."""
    assert "look it up" in BY_NAME["web"].guidance
    assert BY_NAME["fs"].guidance == ""
    assert BY_NAME["compute"].guidance == ""
