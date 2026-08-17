"""Where the live settings table comes from: a toolset's declared settings reaching config.toml."""

from __future__ import annotations

import pytest

from kokua.cli import build_arg_parser, resolve_config
from kokua.config import paths
from kokua.config import file as settings
from kokua.config import settings_sources
from kokua.config.schema import AssistantConfig
from kokua.config.settings_sources import (
    build_settings_table,
    declaring_toolsets,
    seed_toolset_defaults,
    startup_schema,
)
from kokua.toolsets.registry import Setting, Toolset

HOT = Setting("rounds", int, 2, hot=True)
COLD = Setting("endpoint", str, "https://example.invalid")


def _toolset(*declared: Setting) -> Toolset:
    return Toolset(name="widgets", description="A test capability.", build=lambda ctx: [], settings=declared)


def _write_config(text: str):
    path = paths.config_path()
    path.write_text(text, encoding="utf-8")
    return path


def _resolve(*argv):
    return resolve_config(build_arg_parser().parse_args(list(argv)))


def test_a_hot_declaration_becomes_a_namespaced_table_entry():
    table = build_settings_table([_toolset(HOT, COLD)])
    entry = table.by_toml("widgets", "rounds")
    assert (entry.wire_key, entry.kind, entry.toolset) == ("widgets.rounds", int, "widgets")
    assert table.is_hot("widgets", "rounds") is True


def test_a_cold_declaration_stays_out_of_the_table_but_still_has_a_schema_entry():
    """A cold setting must parse and reject a wrong type; it just cannot change without a restart."""
    table = build_settings_table([_toolset(HOT, COLD)])
    assert table.by_toml("widgets", "endpoint") is None
    assert table.is_hot("widgets", "endpoint") is False
    assert startup_schema([_toolset(HOT, COLD)]) == {
        ("widgets", "endpoint"): ("widgets.endpoint", (str,), "a string", None)
    }


def test_kokuas_own_settings_survive_a_contributed_table():
    assert build_settings_table([_toolset(HOT)]).by_field("show_tools") is not None


def test_seeding_fills_a_declared_default_without_overwriting_what_the_file_set():
    config = AssistantConfig(toolset_settings={"widgets": {"rounds": 9}})

    seed_toolset_defaults(config, [_toolset(HOT, COLD)])

    assert config.toolset_settings["widgets"] == {"rounds": 9, "endpoint": COLD.default}


def test_seeding_leaves_no_bucket_for_a_toolset_that_declares_nothing():
    config = AssistantConfig()
    seed_toolset_defaults(config, [_toolset()])
    assert config.toolset_settings == {}


def test_kokuas_core_toolsets_are_a_declaring_source():
    from kokua.toolsets.core import CORE_TOOLSETS

    assert {t.name for t in CORE_TOOLSETS} <= {t.name for t in declaring_toolsets()}


def test_an_mcp_server_cannot_own_a_config_section():
    """An MCP toolset's existence comes *from* the config file, so it cannot contribute to the schema
    that parses that file: naming a section after one is an unknown key, not a settings section."""
    _write_config('[[mcp.server]]\nurl = "https://broker/mcp"\nname = "stocks"\n\n[stocks]\nrounds = 3\n')

    with pytest.raises(settings.ConfigError, match=r"unknown config key \[stocks\].rounds"):
        _resolve()


def test_a_declared_section_parses_and_is_seeded_and_recorded_as_configured(monkeypatch):
    monkeypatch.setattr(settings_sources, "declaring_toolsets", lambda: [_toolset(HOT, COLD)])
    _write_config('[widgets]\nrounds = 5\nendpoint = "https://set/by-hand"\n')

    config = _resolve()

    assert config.toolset_settings["widgets"] == {"rounds": 5, "endpoint": "https://set/by-hand"}
    assert config.configured_sections == ("widgets",)


def test_a_wrong_typed_cold_declaration_is_rejected(monkeypatch):
    monkeypatch.setattr(settings_sources, "declaring_toolsets", lambda: [_toolset(HOT, COLD)])
    _write_config("[widgets]\nendpoint = 3\n")

    with pytest.raises(settings.ConfigError, match=r"\[widgets\].endpoint must be a string"):
        _resolve()


def test_a_section_only_kokua_defaulted_is_not_reported_as_configured(monkeypatch):
    """Seeding fills every declared key, so afterwards only ``configured_sections`` can still tell a
    section the user wrote from one Kokua supplied."""
    monkeypatch.setattr(settings_sources, "declaring_toolsets", lambda: [_toolset(HOT, COLD)])
    _write_config("[display]\nshow_tools = false\n")

    config = _resolve()

    assert config.toolset_settings["widgets"] == {"rounds": HOT.default, "endpoint": COLD.default}
    assert config.configured_sections == ()
