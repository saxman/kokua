"""SettingsApplier: reading, applying, and persisting the runtime-mutable settings."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import pytest


from kokua.config import AssistantConfig
from kokua.config.table import CORE_RUNTIME_SETTINGS, RuntimeSetting, SettingsTable
from kokua.core.assistant import Assistant
from kokua.core.settings_runtime import SettingsApplier
from kokua.core.turn_gate import TurnGate
from tests.channels import FakeChannel, _config, planning_settings
from tests.helpers import MockAsyncModelClient, core_table


def _applier(config, table):
    return SettingsApplier(
        config,
        TurnGate(lambda conversation_id: None),
        table=table,
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
    """config.toml is the single source: what it declares is what the live config holds at boot.

    A toolset's section rather than a core one, since Kokua's core declares no runtime settings now that
    the display flags are gone -- and a contributed setting is the case with a bucket to reach into, so it
    is the one worth asserting.
    """
    cfg = _config(tmp_path, toolset_settings=planning_settings(plan_review=True))
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    assert assistant._config.toolset_settings["planning"]["plan_review"] is True


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
    await assistant.apply_settings({"planning.plan_review": True})
    assert assistant._config.toolset_settings["planning"]["plan_review"] is True
    saved = settings.load(str(cfg.config_path), table=assistant._settings_table)
    assert saved["toolset_settings"]["planning"]["plan_review"] is True


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

    def changed(setting):
        """A value of the setting's own type that differs from what it holds, so a no-op can't pass.

        Typed per kind rather than ``not current``: the sanitizer drops a bool handed to an int
        setting, which would look like a setting that failed to persist.
        """
        current = setting.read(cfg)
        if setting.kind is int:
            return (current or 0) + 7
        if setting.kind is str:
            return f"{current or ''}-changed"
        return not current

    payload: dict = {}
    expected = {}
    for setting in table.settings:
        expected[setting] = changed(setting)
        payload[setting.wire_key] = expected[setting]

    await assistant.apply_settings(payload)

    written = tomllib.loads(cfg.config_path.read_text(encoding="utf-8"))
    for setting, value in expected.items():
        assert setting.read(cfg) == value, f"{setting.wire_key} was not applied to the live config"
        assert written[setting.section][setting.toml_key] == value, (
            f"[{setting.section}].{setting.toml_key} did not persist to config.toml"
        )


async def test_update_config_tool_applies_a_hot_key_live_and_persists(tmp_path):
    from kokua.config import file as settings

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    update = next(t for t in assistant._agent.tools if t.__name__ == "update_config")
    result = await update(section="planning", key="plan_review", value="true")
    live = assistant._config.toolset_settings["planning"]["plan_review"]
    assert live is True  # applied to the live session
    saved = settings.load(str(cfg.config_path), table=assistant._settings_table)
    assert saved["toolset_settings"]["planning"]["plan_review"] is True  # persisted
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


async def test_current_settings_reports_effective(tmp_path):
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(_config(tmp_path, model="m1"), FakeChannel(), client=client)
    s = assistant.current_settings()
    assert "planning.plan_review" in s and "planning.show_reasoning" in s
    assert "generate_kwargs" not in s  # sampling is AIMU's, not a panel field


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


async def test_the_model_is_not_a_runtime_setting(tmp_path):
    """An agent's model comes from its own [agents.*] table or the [assistant].model default, both read
    at startup. A panel field that could disagree with a table the panel cannot write is the conflict
    this removal avoids."""
    assistant = await Assistant.create(_config(tmp_path, model="m1"), FakeChannel(), client=MockAsyncModelClient([]))
    assert "model" not in assistant.current_settings()


async def test_update_config_writes_the_model_for_the_next_restart(tmp_path):
    from kokua.config import file as settings

    cfg = _config(tmp_path, model="m1")
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    update = next(t for t in assistant._agent.tools if t.__name__ == "update_config")
    result = await update(section="assistant", key="model", value="ollama:qwen3:8b")
    assert settings.load(str(cfg.config_path), table=core_table())["model"] == "ollama:qwen3:8b"
    assert assistant._config.model == "m1"  # the running conversations keep the client they were built with
    assert "restart" in result.lower()


# --- applying one hot setting from inside a turn ---------------------------------------------------
#
# `update_config` is a tool, a tool runs inside its turn, and a turn holds that conversation's reader
# on the gate for its whole length. So the one caller `apply_one` has always calls it from under a
# reader, which is exactly the shape `apply`'s exclusive hold cannot survive: the writer waits for the
# reader count to reach zero, and the count includes the turn that is waiting on this call.


def _real_gate() -> TurnGate:
    """A gate whose per-conversation locks are real, so ``gate.turn`` can actually be taken.

    The module's other tests use a lock factory answering None, which is enough for ``apply`` (the
    exclusive path never asks for a conversation's lock) and not for anything holding a turn.
    """
    locks: dict = {}
    return TurnGate(lambda conversation_id: locks.setdefault(conversation_id, asyncio.Lock()))


async def test_applying_one_setting_does_not_wait_on_the_turn_calling_it(tmp_path: Path):
    """The regression. A single-key write reaches one scalar, so it takes no gate hold at all, and the
    turn dispatching the tool is not waiting on itself."""
    config = AssistantConfig(config_path=tmp_path / "config.toml", toolset_settings={"widgets": {"verbose": False}})
    applier = SettingsApplier(config, _real_gate(), table=_table(), state=lambda: None)

    async with applier._gate.turn("c1"):
        await asyncio.wait_for(applier.apply_one("widgets", "verbose", True), timeout=5)

    assert config.toolset_settings["widgets"]["verbose"] is True


async def test_applying_a_whole_payload_still_drains_in_flight_turns(tmp_path: Path):
    """The other half of the asymmetry, so the fix above cannot quietly become "no settings write ever
    synchronizes". A panel payload is several keys, and a turn must not read it half applied."""
    config = AssistantConfig(config_path=tmp_path / "config.toml", toolset_settings={"widgets": {"verbose": False}})
    gate = _real_gate()
    applier = SettingsApplier(config, gate, table=_table(), state=lambda: None)

    async with gate.turn("c1"):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(applier.apply(_table().sanitize({"widgets.verbose": True})), timeout=0.5)


async def test_the_update_config_tool_applies_a_hot_key_from_inside_a_turn(tmp_path):
    """End to end, through the real tool on a real agent. The suite's other coverage of ``update_config``
    stubs ``apply_hot``, so the hold this used to take was never entered and the deadlock never showed."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))
    conversation_id = assistant._active_id
    agent = assistant._book.agent_for(conversation_id)
    update_config = next(fn for fn in agent.tools if getattr(fn, "__name__", "") == "update_config")

    async with assistant._gate.turn(conversation_id):
        answer = await asyncio.wait_for(update_config("assistant", "generate_titles", "false"), timeout=5)

    assert "applied it to the current session" in answer
    assert assistant._config.generate_titles is False


async def test_a_hot_key_applied_in_one_turn_does_not_block_another_conversation(tmp_path):
    """The amplification, pinned. The gate is writer-preferring, so while a settings write waits for a
    reader that will never be released, every *other* conversation's next turn queues behind it too: one
    tool call on one conversation stopped the whole assistant rather than only its own."""
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["ok"])
    )
    busy = assistant._active_id
    await assistant.new_conversation()
    other = assistant._active_id
    agent = assistant._book.agent_for(busy)
    update_config = next(fn for fn in agent.tools if getattr(fn, "__name__", "") == "update_config")

    async def turn_calling_the_tool():
        async with assistant._gate.turn(busy):
            await update_config("assistant", "generate_titles", "false")
            # Held open past the call, so the assertion below is about the settings write and not about
            # a turn that had already finished.
            await asyncio.sleep(0.2)

    running = asyncio.create_task(turn_calling_the_tool())
    await asyncio.sleep(0.05)

    async def unrelated_turn():
        async with assistant._gate.turn(other):
            return "ran"

    assert await asyncio.wait_for(unrelated_turn(), timeout=5) == "ran"
    await running
