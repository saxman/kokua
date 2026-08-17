"""The settings table: core entries plus whatever a toolset contributed, in one lookup.

The `test_every_core_runtime_setting_*` tests are the enforcement mechanism for the table's promise:
adding a core runtime setting is one CORE_RUNTIME_SETTINGS entry, one AssistantConfig field, and one
web-panel input. They fail loudly if an entry is added without its counterparts.
"""

from __future__ import annotations

import re

from kokua.config import AssistantConfig
from kokua.config import file as settings
from kokua.config.table import (
    CORE_RUNTIME_SETTINGS,
    GENERATION_KEYS,
    GENERATION_SETTINGS,
    RuntimeSetting,
    SettingsTable,
)


def _table() -> SettingsTable:
    return SettingsTable(
        [
            *CORE_RUNTIME_SETTINGS,
            RuntimeSetting("plan_review", "planning", bool, toolset="planning"),
            RuntimeSetting("review_rounds", "planning", int, toolset="planning"),
        ]
    )


def test_a_contributed_setting_is_hot_and_findable_by_its_toml_location():
    table = _table()
    assert table.is_hot("planning", "plan_review") is True
    assert table.by_toml("planning", "plan_review").toolset == "planning"


def test_a_core_setting_keeps_a_bare_wire_key():
    assert SettingsTable(CORE_RUNTIME_SETTINGS).by_field("show_tools").wire_key == "show_tools"


def test_a_contributed_setting_is_namespaced_on_the_wire():
    assert _table().by_toml("planning", "plan_review").wire_key == "planning.plan_review"


def test_sanitize_accepts_both_wire_shapes_and_drops_junk():
    cleaned = _table().sanitize(
        {
            "show_tools": True,
            "planning.plan_review": True,
            "planning.review_rounds": "not an int",
            "unknown": 1,
        }
    )
    assert cleaned["show_tools"] is True
    assert cleaned["planning.plan_review"] is True
    assert "planning.review_rounds" not in cleaned
    assert "unknown" not in cleaned
    assert cleaned["generate_kwargs"] == {}


def test_a_contributed_setting_reads_and_writes_the_toolset_bucket():
    config = AssistantConfig(toolset_settings={"planning": {"plan_review": False}})
    setting = _table().by_toml("planning", "plan_review")

    assert setting.read(config, lambda name, default: default) is False
    setting.write(config, True, lambda name, value: None)
    assert config.toolset_settings["planning"]["plan_review"] is True


def test_a_core_setting_still_reads_and_writes_a_config_attribute():
    config = AssistantConfig()
    setting = SettingsTable(CORE_RUNTIME_SETTINGS).by_field("show_tools")

    setting.write(config, False, lambda name, value: None)
    assert config.show_tools is False


def test_the_toml_schema_covers_contributed_sections():
    schema = _table().toml_schema()
    assert ("planning", "plan_review") in schema
    assert schema[("planning", "review_rounds")][1] == (int,)


def test_every_core_runtime_setting_is_an_assistant_config_field():
    config = AssistantConfig()
    for setting in CORE_RUNTIME_SETTINGS:
        assert hasattr(config, setting.field), f"{setting.field} is not an AssistantConfig field"


def test_core_runtime_settings_do_not_collide_with_startup_only_keys():
    """A table entry that shadows a startup-only schema key would silently win, changing that key's
    type and making it hot. The table's half of the built schema must not overlap the hand-written half."""
    generated = {(s.section, s.toml_key) for s in CORE_RUNTIME_SETTINGS}
    assert not (generated & set(settings._STARTUP_SCHEMA)), "a runtime setting shadows a startup-only key"


def _example_keys() -> set[tuple[str, str]]:
    """The (section, key) pairs config.example.toml documents, including commented-out defaults."""
    pairs: set[tuple[str, str]] = set()
    section = ""
    for line in settings.example_text().splitlines():
        stripped = line.lstrip("#").strip()
        header = re.fullmatch(r"\[([\w.]+)\]", stripped)
        if header:
            section = header.group(1)
            continue
        assignment = re.match(r"(\w+)\s*=", stripped)
        if assignment and section:
            pairs.add((section, assignment.group(1)))
    return pairs


def test_every_core_runtime_setting_is_documented_under_its_own_section():
    """config.example.toml documents every key at its default (commented out counts as documented).

    Section-aware on purpose: the example file is hand-maintained and so is the only source of truth
    independent of the table. A merely name-based check would pass for an entry filed under the wrong
    [section], which is exactly the mistake that makes a setting silently fail to persist.
    """
    documented = _example_keys()
    for setting in CORE_RUNTIME_SETTINGS:
        assert (setting.section, setting.toml_key) in documented, (
            f"[{setting.section}].{setting.toml_key} is not documented in config.example.toml"
        )
    for generation in GENERATION_SETTINGS:
        assert ("generation", generation.field) in documented, (
            f"[generation].{generation.field} is not documented in config.example.toml"
        )


def test_generation_keys_match_the_generation_table():
    assert GENERATION_KEYS == tuple(s.field for s in GENERATION_SETTINGS)


def test_sanitize_drops_unknown_and_coerces_types():
    result = _table().sanitize({"generate_kwargs": {"temperature": "0.5", "max_tokens": 10, "bogus": 1}})
    assert result["generate_kwargs"] == {"temperature": 0.5, "max_tokens": 10}


def test_sanitize_drops_out_of_range_and_none():
    result = _table().sanitize({"generate_kwargs": {"temperature": 5.0, "top_p": 0.9, "top_k": None}})
    assert result["generate_kwargs"] == {"top_p": 0.9}  # temperature out of [0,2], top_k None


def test_sanitize_rejects_bools_for_numeric_kwargs():
    result = _table().sanitize({"generate_kwargs": {"temperature": True}})
    assert result["generate_kwargs"] == {}


def test_sanitize_model_and_flags():
    result = _table().sanitize({"model": "  anthropic:x  ", "show_thinking": True, "show_tools": "yes"})
    assert result["model"] == "anthropic:x"  # trimmed
    assert result["show_thinking"] is True
    assert "show_tools" not in result  # non-bool dropped


def test_sanitize_keeps_plan_flags():
    result = SettingsTable(CORE_RUNTIME_SETTINGS).sanitize(
        {"plan_review_agent": True, "plan_review": False, "plan_bogus": True}
    )
    assert result["plan_review_agent"] is True
    assert result["plan_review"] is False
    assert "plan_bogus" not in result


def test_sanitize_blank_model_omitted():
    assert "model" not in _table().sanitize({"model": "   "})


def test_sanitize_always_has_generate_kwargs():
    assert _table().sanitize({}) == {"generate_kwargs": {}}
