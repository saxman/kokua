"""Unit tests for the runtime-settings JSON store (load / save / sanitize)."""

from __future__ import annotations

from kokua import runtime_settings


def test_load_missing_returns_empty(tmp_path):
    assert runtime_settings.load(tmp_path / "nope.json") == {}


def test_load_corrupt_returns_empty(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    assert runtime_settings.load(path) == {}


def test_load_non_dict_returns_empty(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert runtime_settings.load(path) == {}


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "sub" / "settings.json"  # parent dir does not exist yet
    settings = {
        "model": "anthropic:claude-sonnet-4-6",
        "show_thinking": False,
        "generate_kwargs": {"temperature": 0.3, "max_tokens": 2048},
    }
    runtime_settings.save(path, settings)
    assert path.exists()  # save created the parent dir
    loaded = runtime_settings.load(path)
    assert loaded["model"] == "anthropic:claude-sonnet-4-6"
    assert loaded["show_thinking"] is False
    assert loaded["generate_kwargs"] == {"temperature": 0.3, "max_tokens": 2048}


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
