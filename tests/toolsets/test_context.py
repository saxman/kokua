"""LiveState's laziness is the mechanism that replaced the `memory = true` config flag, so it is worth
pinning directly: shared across agents when declared, never constructed when not."""

from kokua.config.schema import AssistantConfig
from kokua.toolsets.context import LiveState, ToolsetContext


def _state(tmp_path) -> LiveState:
    return LiveState(
        config=AssistantConfig(data_dir=tmp_path),
        notify=None,
        oauth_storage_dir=tmp_path / "mcp-oauth",
        connections=[],
        for_each_agent=lambda apply: None,
        reapply_config=None,
    )


def test_memory_store_is_one_object_across_two_agent_contexts(tmp_path):
    state = _state(tmp_path)
    first = ToolsetContext(state=state, agent=object())
    second = ToolsetContext(state=state, agent=object())
    assert first.state.memory_store is second.state.memory_store


def test_memory_store_is_not_constructed_until_asked_for(tmp_path):
    state = _state(tmp_path)
    assert "memory_store" not in state.__dict__
    assert not (tmp_path / "memory").exists()


def test_context_passes_config_through(tmp_path):
    state = _state(tmp_path)
    ctx = ToolsetContext(state=state, agent=object())
    assert ctx.config is state.config


def test_skill_manager_is_cached(tmp_path):
    state = _state(tmp_path)
    assert state.skill_manager is state.skill_manager
