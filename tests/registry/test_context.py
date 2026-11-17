"""LiveState's laziness is the mechanism that replaced the `memory = true` config flag, so it is worth
pinning directly: shared across agents when declared, never constructed when not."""

from kokua.config.schema import AssistantConfig
from kokua.registry.context import LiveState, ToolsetContext
from kokua.mcp.auth import OAuthSettings


def _state(tmp_path) -> LiveState:
    return LiveState(
        config=AssistantConfig(data_dir=tmp_path),
        notify=None,
        oauth=OAuthSettings(storage_dir=tmp_path / "mcp-oauth"),
        connections=[],
        for_each_agent=lambda apply: None,
        reapply_config=None,
    )


def test_memory_store_is_one_object_across_two_agent_contexts(tmp_path):
    state = _state(tmp_path)
    first = ToolsetContext(state=state, agent=object(), agent_name="assistant")
    second = ToolsetContext(state=state, agent=object(), agent_name="assistant")
    assert first.state.memory_store is second.state.memory_store


def test_memory_store_is_not_constructed_until_asked_for(tmp_path):
    state = _state(tmp_path)
    assert "memory_store" not in state.__dict__
    assert not (tmp_path / "memory").exists()


def test_context_passes_config_through(tmp_path):
    state = _state(tmp_path)
    ctx = ToolsetContext(state=state, agent=object(), agent_name="assistant")
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


def test_context_names_the_agent_it_is_building_for(tmp_path):
    """The name, unlike the live ``agent`` object, is known at every construction site. It is what lets a
    toolset ask the config what its own holder runs on: a live client does not retain the model string it
    was built from, so the object could not answer even where it is present."""
    ctx = ToolsetContext(state=_state(tmp_path), agent=None, agent_name="researcher")
    assert ctx.agent_name == "researcher"


class _SlowStore:
    """A document store whose close joins a busy index worker, as AIMU's does for up to ten seconds."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.closed = False

    def close(self) -> None:
        import time

        time.sleep(self.seconds)
        self.closed = True


async def test_aclose_closes_document_stores_without_blocking_the_event_loop(tmp_path):
    """In the web front end a LiveState lives per connection, so a reload during a first index build
    closes stores on the server's one loop. Closing them there would stall every other connection."""
    import asyncio
    import time

    state = _state(tmp_path)
    stores = [_SlowStore(0.4), _SlowStore(0.4)]
    state._document_stores = {"a": stores[0], "b": stores[1]}

    started = time.monotonic()
    closing = asyncio.create_task(state.aclose())
    await asyncio.sleep(0.05)
    assert time.monotonic() - started < 0.2, "the loop stalled while the stores closed"
    assert not closing.done()
    await closing
    assert all(store.closed for store in stores)
    assert time.monotonic() - started < 0.75, "the stores closed one after another rather than together"


def test_close_closes_every_document_store_together(tmp_path):
    import time

    state = _state(tmp_path)
    stores = [_SlowStore(0.3), _SlowStore(0.3), _SlowStore(0.3)]
    state._document_stores = dict(zip("abc", stores))

    started = time.monotonic()
    state.close()
    assert all(store.closed for store in stores)
    assert time.monotonic() - started < 0.6
