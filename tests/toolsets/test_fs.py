"""One AIMU group, two toolsets, split by reach.

AIMU 0.31.0 put ``write_file`` and ``edit_file`` *into* ``builtin.fs``, so the wrapper that used to be
``list(builtin.fs)`` would have granted a write to every agent already declaring ``fs``, on an upgrade,
with no config change and nothing reported. These tests pin the split that answers it and, more
importantly, pin the property that makes it worth having: that the two halves are a partition, computed
from AIMU's own names rather than from a list of tools spelled in this repository.
"""

from __future__ import annotations

from aimu.tools import builtin as aimu_builtin

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.registry import LiveState, ToolsetContext
from kokua.toolsets.fs import TOOLSET as FS
from kokua.toolsets.fs_write import TOOLSET as FS_WRITE


def _ctx(tmp_path, agent_name="assistant"):
    return ToolsetContext(state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=None, agent_name=agent_name)


def test_fs_holds_no_tool_that_writes(tmp_path):
    """The one property this toolset exists for. Asserted against ``builtin.unscoped`` rather than
    against a list of names, so a writer AIMU adds to the group later fails here too."""
    built = FS.build(_ctx(tmp_path))

    assert {fn.__name__ for fn in built} == {"list_directory", "read_file"}
    assert not {fn.__name__ for fn in built} & {fn.__name__ for fn in aimu_builtin.unscoped}


def test_fs_write_holds_exactly_the_writers(tmp_path):
    assert {fn.__name__ for fn in FS_WRITE.build(_ctx(tmp_path))} == {"write_file", "edit_file"}


def test_the_two_toolsets_partition_the_aimu_group(tmp_path):
    """Together they are the whole group and separately they do not overlap, which is what makes
    declaring both equivalent to the ``list(builtin.fs)`` this pair replaced. A tool AIMU adds to ``fs``
    therefore cannot go missing from Kokua by nobody noticing: it lands in whichever half its reach
    puts it in."""
    read = {fn.__name__ for fn in FS.build(_ctx(tmp_path))}
    write = {fn.__name__ for fn in FS_WRITE.build(_ctx(tmp_path))}

    assert read | write == {fn.__name__ for fn in aimu_builtin.fs}
    assert not read & write


def test_the_shipped_introspector_reads_files_and_cannot_write_them(tmp_path):
    """The agent the split was written for. It declares ``fs`` to read an export it was asked to
    evaluate, and evaluating a conversation is no reason to be able to rewrite one."""
    from kokua.core.agents import build_agent_specs, build_registry

    config = AssistantConfig(data_dir=tmp_path)
    config.agents = {
        "assistant": AgentConfig(tools=[], delegates_to=["introspector"]),
        "introspector": AgentConfig(description="Evaluates a conversation.", tools=["fs", "time"]),
    }
    state = LiveState(config=config, registry=build_registry(config))

    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["introspector"]["tools"]}

    assert "read_file" in names
    assert not names & {"write_file", "edit_file"}


def test_a_writer_is_reachable_only_by_declaring_the_write_toolset(tmp_path):
    """Capability is declared, never defaulted: the writers arrive when an agent's own ``tools`` names
    ``fs_write`` and by no other route."""
    from kokua.core.agents import build_agent_specs, build_registry

    config = AssistantConfig(data_dir=tmp_path)
    config.agents = {
        "assistant": AgentConfig(tools=[], delegates_to=["coder"]),
        "coder": AgentConfig(description="Writes code.", tools=["fs", "fs_write"]),
    }
    state = LiveState(config=config, registry=build_registry(config))

    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["coder"]["tools"]}

    assert {"read_file", "write_file", "edit_file"} <= names


def test_neither_toolset_adds_guidance():
    """``web`` is the one AIMU group Kokua adds guidance to, because its trigger is epistemic rather
    than written on the tool. ``write_file``'s own schema already says it replaces a whole file and
    names ``edit_file`` as the alternative, so a second copy would be prompt tokens spent per request
    to repeat what the model reads on the tool."""
    assert FS.guidance == ""
    assert FS_WRITE.guidance == ""
