"""Each core subsystem declares its own toolset next to the tools it wraps."""

import subprocess
import sys

from kokua.config.schema import AssistantConfig
from kokua.registry.context import LiveState, ToolsetContext
from kokua.toolsets.core import CORE_TOOLSETS
from kokua.mcp.auth import OAuthSettings

BY_NAME = {t.name: t for t in CORE_TOOLSETS}


def _ctx(tmp_path, **state_kwargs) -> ToolsetContext:
    state = LiveState(config=AssistantConfig(data_dir=tmp_path), **state_kwargs)
    return ToolsetContext(state=state, agent=object())


def test_the_six_core_toolsets_are_collected():
    assert sorted(BY_NAME) == ["capabilities", "config", "conversations", "mcp-admin", "planning", "scheduling"]


def test_importing_the_core_toolsets_does_not_pull_the_preflight_surface():
    """`aimu.aio.tools.builtin` is the AIMU surface `aimu_compat.require_aimu` probes, and this module is
    on the import path of `resolve_config`, which runs before that preflight on invocations such as
    `kokua skills install`. Importing the surface here would turn an AIMU checkout missing it into a bare
    ImportError at that point, instead of the actionable message the preflight prints, so any core toolset
    needing it must import it inside the function that calls it.

    Run in a child interpreter because this one has already imported the surface: the test suite exercises
    the composition path directly, so an in-process `sys.modules` check would say nothing about the import
    graph.
    """
    probe = "import sys; import kokua.toolsets.core; print('aimu.aio.tools.builtin' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"


def test_the_planning_toolset_carries_a_workflow_and_no_tools(tmp_path):
    """The capability `Toolset.workflow` exists for: `/plan` is granted by declaring "planning" in an
    agent's `tools`, and nothing about it reaches the model as a tool."""
    assert BY_NAME["planning"].workflow is not None
    assert BY_NAME["planning"].build(_ctx(tmp_path)) == []


def test_every_core_toolset_is_cross_cutting():
    assert all(t.cross_cutting for t in CORE_TOOLSETS)


def test_no_core_toolset_is_entry_point_only():
    assert not any(t.entry_point_only for t in CORE_TOOLSETS)


def test_config_toolset_builds_the_read_and_update_tools(tmp_path):
    ctx = _ctx(tmp_path, reapply_config=lambda *a: None)
    assert {fn.__name__ for fn in BY_NAME["config"].build(ctx)} == {"read_config", "update_config"}


def test_mcp_admin_builds_the_add_and_remove_tools(tmp_path):
    ctx = _ctx(
        tmp_path,
        for_each_agent=lambda apply: None,
        oauth=OAuthSettings(storage_dir=tmp_path),
        refresh_workers=lambda agent: None,
    )
    assert {fn.__name__ for fn in BY_NAME["mcp-admin"].build(ctx)} == {"add_mcp_server", "remove_mcp_server"}


def test_conversations_toolset_builds_the_three_read_only_tools(tmp_path):
    class FakeBook:
        pass

    ctx = _ctx(tmp_path, conversation_book=FakeBook(), turn_running=lambda cid: False)
    names = {fn.__name__ for fn in BY_NAME["conversations"].build(ctx)}
    assert names == {"list_conversations", "read_conversation", "search_conversations"}


def test_conversations_guidance_names_the_cross_conversation_tools():
    guidance = BY_NAME["conversations"].guidance
    assert "list_conversations" in guidance
    assert "read_conversation" in guidance
    assert "search_conversations" in guidance
