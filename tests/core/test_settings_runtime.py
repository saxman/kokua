"""SettingsApplier: reading, applying, and persisting the runtime-mutable settings."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aimu.aio.channels.base import ChannelMessage

from kokua.config import AssistantConfig
from kokua.config.table import CORE_RUNTIME_SETTINGS, RuntimeSetting, SettingsTable
from kokua.core.assistant import Assistant
from kokua.core.settings_runtime import SettingsApplier
from kokua.core.turn_gate import TurnGate
from tests.channels import FakeChannel, _config
from tests.helpers import MockAsyncModelClient, core_table


class _UI:
    def display_flag(self, name, default):
        return default

    def set_display_flag(self, name, value):
        return None


class _Agent:
    class _Client:
        default_generate_kwargs: dict = {}
        messages: list = []

    model_client = _Client()


def _applier(config, table):
    return SettingsApplier(
        config,
        _UI(),
        TurnGate(lambda conversation_id: None),
        table=table,
        live_agents=lambda: [],
        cached_ids=lambda: [],
        agent_for=lambda conversation_id: _Agent(),
        active_agent=lambda: _Agent(),
        cancel_active_turn=None,
        state=lambda: None,
    )


def _table():
    """The core table plus a third party's one hot setting. A fictitious ``widgets`` rather than the real
    ``planning``, which also declares a contributed setting now, so it would work here too -- but a test
    double keeps this table from changing shape if the shipped toolset's declarations ever do."""
    return SettingsTable([*CORE_RUNTIME_SETTINGS, RuntimeSetting("verbose", "widgets", bool, toolset="widgets")])


async def test_applying_a_contributed_setting_reaches_the_live_config(tmp_path: Path):
    config = AssistantConfig(config_path=tmp_path / "config.toml", toolset_settings={"widgets": {"verbose": False}})
    applier = _applier(config, _table())

    await applier.apply(_table().sanitize({"widgets.verbose": True}))

    assert config.toolset_settings["widgets"]["verbose"] is True


def test_persisting_a_contributed_setting_writes_its_own_section(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = AssistantConfig(config_path=path, toolset_settings={"widgets": {}})
    table = _table()

    _applier(config, table).persist(table.sanitize({"widgets.verbose": True}))

    assert tomllib.loads(path.read_text())["widgets"]["verbose"] is True


def test_the_panel_payload_carries_a_contributed_setting_namespaced(tmp_path: Path):
    config = AssistantConfig(config_path=tmp_path / "config.toml", toolset_settings={"widgets": {"verbose": True}})

    current = _applier(config, _table()).current()

    assert current["widgets.verbose"] is True


async def test_boot_applies_config_flags(tmp_path):
    # config.toml is the single source: its [display] values apply at boot.
    cfg = _config(tmp_path, show_tools=False)
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    assert assistant._config.show_tools is False


async def test_boot_leaves_sampling_parameters_to_aimu(tmp_path):
    """Kokua no longer writes ``default_generate_kwargs``, so AIMU's own tier chain decides a request.

    Writing it shadowed the model card's tuned profile, which AIMU layers underneath that tier.
    """
    client = MockAsyncModelClient([])
    await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    assert client.default_generate_kwargs == {}


async def test_boot_does_not_write_config(tmp_path):
    cfg = _config(tmp_path)
    before = cfg.config_path.read_text(encoding="utf-8")
    await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    # config.toml is required, so it always exists; "not written" means byte-identical after boot.
    assert cfg.config_path.read_text(encoding="utf-8") == before


async def test_apply_settings_updates_and_persists_to_config(tmp_path):
    from kokua.config import file as settings

    cfg = _config(tmp_path)
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    await assistant.apply_settings({"show_tools": False})
    assert assistant._config.show_tools is False
    saved = settings.load(str(cfg.config_path), table=core_table())
    assert saved["show_tools"] is False


async def test_every_runtime_setting_round_trips_through_config_toml(tmp_path):
    """The table's promise, end to end: for EVERY entry in this process's live settings table, a panel
    payload applies to the live config and persists to config.toml under the entry's own [section].key.

    This is what makes adding a setting a one-line change: a new entry (Kokua's own or a toolset's) is
    covered here automatically, and an entry whose section/key is wrong fails immediately instead of
    silently not persisting.
    """
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    table = assistant._settings_table

    def unmirrored(name, default):
        return default

    def changed(setting):
        """A value of the setting's own type that differs from what it holds, so a no-op can't pass.

        Typed per kind rather than ``not current``: the sanitizer drops a bool handed to an int
        setting, which would look like a setting that failed to persist.
        """
        current = setting.read(cfg, unmirrored)
        if setting.kind is int:
            return (current or 0) + 7
        if setting.kind is str:
            return f"{current or ''}-changed"
        return not current

    payload: dict = {}
    expected = {}
    for setting in table.settings:
        if setting.wire_key == "model":
            continue  # switching the model rebuilds the client; covered by its own test
        expected[setting] = changed(setting)
        payload[setting.wire_key] = expected[setting]

    await assistant.apply_settings(payload)

    written = tomllib.loads(cfg.config_path.read_text(encoding="utf-8"))
    for setting, value in expected.items():
        assert setting.read(cfg, unmirrored) == value, f"{setting.wire_key} was not applied to the live config"
        assert written[setting.section][setting.toml_key] == value, (
            f"[{setting.section}].{setting.toml_key} did not persist to config.toml"
        )


async def test_update_config_tool_applies_a_hot_key_live_and_persists(tmp_path):
    from kokua.config import file as settings

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    update = next(t for t in assistant._agent.tools if t.__name__ == "update_config")
    result = await update(section="display", key="show_tools", value="false")
    assert assistant._config.show_tools is False  # applied to the live session
    assert settings.load(str(cfg.config_path), table=core_table())["show_tools"] is False  # persisted
    assert "restart" not in result.lower()


async def test_update_config_tool_restart_key_writes_without_applying(tmp_path):
    from kokua.config import file as settings

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    update = next(t for t in assistant._agent.tools if t.__name__ == "update_config")
    result = await update(section="logging", key="level", value="DEBUG")
    assert settings.load(str(cfg.config_path), table=core_table())["log_level"] == "DEBUG"
    assert "restart" in result.lower()


async def test_update_config_tool_refuses_blocklisted_key(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    update = next(t for t in assistant._agent.tools if t.__name__ == "update_config")
    before = cfg.config_path.read_text(encoding="utf-8")
    result = await update(section="security", key="confirm_tools", value="[]")
    assert cfg.config_path.read_text(encoding="utf-8") == before  # nothing written
    assert "hand-edit" in result.lower()


async def test_apply_settings_switches_model(tmp_path, monkeypatch):
    first = MockAsyncModelClient(["hi"])
    assistant = await Assistant.create(_config(tmp_path, model="m1"), FakeChannel(), client=first)
    await assistant._handle(
        ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id
    )  # populate conversation state

    second = MockAsyncModelClient([])
    monkeypatch.setattr("kokua.core.assistant.aio.client", lambda *a, **k: second)

    await assistant.apply_settings({"model": "m2"})

    assert assistant._agent.model_client is second
    assert assistant._config.model == "m2"
    # conversation restored onto the new client (system message stripped, the user turn preserved)
    assert any(m.get("content") == "hello" for m in second.messages)


async def test_current_settings_reports_effective(tmp_path):
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(_config(tmp_path, model="m1"), FakeChannel(), client=client)
    s = assistant.current_settings()
    assert s["model"] == "m1"
    assert "show_thinking" in s and "show_tools" in s
    assert "generate_kwargs" not in s  # sampling is AIMU's, not a panel field


async def test_model_switch_applies_to_all_live_agents(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    second = await assistant.new_conversation()  # noqa: F841
    await assistant.select_conversation(first)

    built = []

    def fake_client(model, system=None):
        c = MockAsyncModelClient([])
        c.model = MagicMock(supports_tools=True, supports_thinking=False, supports_vision=False)
        built.append(model)
        return c

    monkeypatch.setattr("kokua.core.settings_runtime.aio.client", fake_client)
    await assistant._settings.switch_model("anthropic:claude-x")
    # Both cached agents got a rebuilt client for the new model.
    assert built.count("anthropic:claude-x") == len(assistant._registry.live_agents())


async def test_new_conversation_agent_gets_an_untouched_client(tmp_path):
    """A lazily-built conversation's client reaches the agent as the factory made it.

    Kokua used to rewrite ``default_generate_kwargs`` on every client the factory returned; nothing
    layers onto it now, so AIMU's own tier chain applies to a new conversation as it does to the first.
    """
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([])
    )
    new_id = await assistant.new_conversation()
    assert assistant._registry.get(new_id).model_client.default_generate_kwargs == {}


async def test_create_wraps_unbuildable_client_as_model_client_error(tmp_path, monkeypatch):
    import kokua.core.assistant as assistant_mod
    from kokua.core.assistant import ModelClientError

    def boom(*args, **kwargs):
        raise ValueError("No model specified and no default could be resolved.")

    monkeypatch.setattr(assistant_mod.aio, "client", boom)
    with pytest.raises(ModelClientError, match="no default could be resolved"):
        await Assistant.create(_config(tmp_path), FakeChannel())
