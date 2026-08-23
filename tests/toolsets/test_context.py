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


# ---------------------------------------------------------------------------
# The host context skill scripts run with. A script cannot discover where Kokua serves downloads from
# or which address it may mail, and every route to a script has to pass the same map, since a route
# that forgets raises nothing: the script just runs with the settings missing and says it is
# unconfigured.
# ---------------------------------------------------------------------------


def test_script_env_carries_the_configured_email_settings(tmp_path):
    state = LiveState(
        config=AssistantConfig(
            data_dir=tmp_path,
            email_host="smtp.example.com",
            email_port=465,
            email_to="user@example.com",
            email_from="sender@example.com",
            email_username="login@example.com",
            email_use_ssl=True,
        )
    )
    env = state.script_env()
    assert env["KOKUA_EMAIL_HOST"] == "smtp.example.com"
    assert env["KOKUA_EMAIL_PORT"] == "465"
    assert env["KOKUA_EMAIL_TO"] == "user@example.com"
    assert env["KOKUA_EMAIL_FROM"] == "sender@example.com"
    assert env["KOKUA_EMAIL_USERNAME"] == "login@example.com"
    assert env["KOKUA_EMAIL_USE_SSL"] == "1"
    assert env["KOKUA_DOWNLOADS_DIR"] == str(state.config.downloads_path)
    assert env["KOKUA_IMAGES_DIR"] == str(state.config.images_path)


def test_script_env_omits_the_password(tmp_path):
    """It is already in this process's environment, which the subprocess inherits, so copying it here
    would duplicate a secret for no gain."""
    state = _state(tmp_path)
    assert not any("PASSWORD" in key for key in state.script_env())


def test_script_env_omits_unset_email_settings(tmp_path):
    """An empty host is not the same as a host set to "", which the script would try to connect to."""
    env = _state(tmp_path).script_env()
    assert "KOKUA_EMAIL_HOST" not in env
    assert "KOKUA_EMAIL_TO" not in env
