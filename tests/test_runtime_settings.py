"""Unit tests for the runtime-settings sanitizer (the web panel's wire payload -> clean dict)."""

from __future__ import annotations

from kokua import runtime_settings


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
