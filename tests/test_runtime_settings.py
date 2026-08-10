"""Unit tests for the runtime-settings table and its sanitizer.

The `test_every_runtime_setting_*` tests are the enforcement mechanism for the table's promise:
adding a runtime setting is one RUNTIME_SETTINGS entry, one AssistantConfig field, and one web-panel
input. They fail loudly if an entry is added without its counterparts.
"""

from __future__ import annotations

import re

from kokua import runtime_settings, settings
from kokua.config import AssistantConfig


def test_every_runtime_setting_is_an_assistant_config_field():
    config = AssistantConfig(memory=False)
    for setting in runtime_settings.RUNTIME_SETTINGS:
        assert hasattr(config, setting.field), f"{setting.field} is not an AssistantConfig field"


def test_runtime_settings_do_not_collide_with_startup_only_keys():
    """A table entry that shadows a startup-only schema key would silently win, changing that key's
    type and making it hot. The generated half of _SCHEMA must not overlap the hand-written half."""
    generated = {(s.section, s.toml_key) for s in runtime_settings.RUNTIME_SETTINGS}
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


def test_every_runtime_setting_is_documented_under_its_own_section():
    """config.example.toml documents every key at its default (commented out counts as documented).

    Section-aware on purpose: the example file is hand-maintained and so is the only source of truth
    independent of the table. A merely name-based check would pass for an entry filed under the wrong
    [section], which is exactly the mistake that makes a setting silently fail to persist.
    """
    documented = _example_keys()
    for setting in runtime_settings.RUNTIME_SETTINGS:
        assert (setting.section, setting.toml_key) in documented, (
            f"[{setting.section}].{setting.toml_key} is not documented in config.example.toml"
        )
    for generation in runtime_settings.GENERATION_SETTINGS:
        assert ("generation", generation.field) in documented, (
            f"[generation].{generation.field} is not documented in config.example.toml"
        )


def test_generation_keys_match_the_generation_table():
    assert runtime_settings.GENERATION_KEYS == tuple(s.field for s in runtime_settings.GENERATION_SETTINGS)


def test_sanitize_drops_unknown_and_coerces_types():
    result = runtime_settings.sanitize({"generate_kwargs": {"temperature": "0.5", "max_tokens": 10, "bogus": 1}})
    assert result["generate_kwargs"] == {"temperature": 0.5, "max_tokens": 10}


def test_sanitize_drops_out_of_range_and_none():
    result = runtime_settings.sanitize({"generate_kwargs": {"temperature": 5.0, "top_p": 0.9, "top_k": None}})
    assert result["generate_kwargs"] == {"top_p": 0.9}  # temperature out of [0,2], top_k None


def test_sanitize_rejects_bools_for_numeric_kwargs():
    result = runtime_settings.sanitize({"generate_kwargs": {"temperature": True}})
    assert result["generate_kwargs"] == {}


def test_sanitize_model_and_flags():
    result = runtime_settings.sanitize({"model": "  anthropic:x  ", "show_thinking": True, "show_tools": "yes"})
    assert result["model"] == "anthropic:x"  # trimmed
    assert result["show_thinking"] is True
    assert "show_tools" not in result  # non-bool dropped


def test_sanitize_keeps_plan_flags():
    result = runtime_settings.sanitize({"plan_review_agent": True, "plan_review": False, "plan_bogus": True})
    assert result["plan_review_agent"] is True
    assert result["plan_review"] is False
    assert "plan_bogus" not in result


def test_sanitize_blank_model_omitted():
    assert "model" not in runtime_settings.sanitize({"model": "   "})


def test_sanitize_always_has_generate_kwargs():
    assert runtime_settings.sanitize({}) == {"generate_kwargs": {}}
