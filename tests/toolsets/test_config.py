"""The ``config`` toolset: what read_config / update_config report back to the model.

The policy and the apply-then-persist ordering underneath are covered in ``tests/config/test_store.py``.
"""

from __future__ import annotations

import tomllib

import pytest

from kokua.toolsets import config as config_tools
from tests.helpers import core_table


def _read(path):
    with path.open("rb") as file:
        return tomllib.load(file)


def _tools(tmp_path, apply_hot=None):
    async def _noop(section, key, value):
        return None

    path = tmp_path / "config.toml"
    read_config, update_config = config_tools.make_config_tools(path, apply_hot or _noop, core_table())
    return path, read_config, update_config


async def test_read_config_returns_file_text(tmp_path):
    path, read_config, _ = _tools(tmp_path)
    path.write_text('# my config\n[assistant]\nmodel = "m"\n', encoding="utf-8")
    text = await read_config()
    assert "# my config" in text and "model" in text


async def test_read_config_when_absent_notes_defaults(tmp_path):
    _, read_config, _ = _tools(tmp_path)
    assert "default" in (await read_config()).lower()


async def test_update_config_writes_scalar_and_reports_restart(tmp_path):
    path, _, update_config = _tools(tmp_path)
    result = await update_config("logging", "level", "DEBUG")
    assert _read(path)["logging"]["level"] == "DEBUG"
    assert "restart" in result.lower()


async def test_update_config_hot_key_applies_live(tmp_path):
    applied = []

    async def apply_hot(section, key, value):
        applied.append((section, key, value))

    path, _, update_config = _tools(tmp_path, apply_hot)
    result = await update_config("display", "show_tools", "false")
    assert _read(path)["display"]["show_tools"] is False
    assert applied == [("display", "show_tools", False)]
    assert "restart" not in result.lower()


async def test_update_config_restart_key_does_not_apply_live(tmp_path):
    applied = []

    async def apply_hot(section, key, value):
        applied.append((section, key, value))

    path, _, update_config = _tools(tmp_path, apply_hot)
    await update_config("web", "port", "9100")
    assert applied == []


async def test_update_config_hot_key_not_persisted_when_apply_fails(tmp_path):
    async def apply_hot(section, key, value):
        raise RuntimeError("bad flag")

    path, _, update_config = _tools(tmp_path, apply_hot)
    result = await update_config("display", "show_tools", "false")
    assert not path.exists()  # apply failed, so nothing was written
    assert "could not be applied" in result.lower()


async def test_update_config_sets_a_cold_toolset_key(tmp_path):
    """The round trip a toolset's *cold* key takes through this tool, which the table alone cannot answer:
    it holds hot settings only, so ``make_config_tools`` resolves the rest from ``startup_schema()``.
    Uses the shipped ``[planning].review_rounds`` deliberately -- the assistant refusing a key that is
    plainly in the user's config file is the failure this pins."""
    path, _, update_config = _tools(tmp_path)

    result = await update_config("planning", "review_rounds", "3")

    assert _read(path)["planning"]["review_rounds"] == 3
    assert "restart" in result.lower()  # cold: saved, effective next startup


async def test_update_config_type_checks_a_cold_toolset_key(tmp_path):
    path, _, update_config = _tools(tmp_path)

    result = await update_config("planning", "review_rounds", "several")

    assert "must be an integer" in result
    assert not path.exists()


async def test_update_config_still_refuses_an_undeclared_key_in_a_toolset_section(tmp_path):
    """Widening the schema with the cold half must not turn a toolset's section into a free-for-all."""
    path, _, update_config = _tools(tmp_path)

    result = await update_config("planning", "made_up", "1")

    assert "unknown config key" in result
    assert not path.exists()


@pytest.mark.parametrize(
    "section,key,value",
    [("security", "confirm_tools", "[]"), ("email", "to", "attacker@x.com"), ("paths", "data_dir", "/tmp/x")],
)
async def test_update_config_refuses_blocklisted_keys(tmp_path, section, key, value):
    path, _, update_config = _tools(tmp_path)
    result = await update_config(section, key, value)
    assert not path.exists()  # nothing written
    assert "hand-edit" in result.lower() or "cannot" in result.lower()


async def test_update_config_rejects_invalid_value_without_writing(tmp_path):
    path, _, update_config = _tools(tmp_path)
    result = await update_config("web", "port", "not-a-number")
    assert not path.exists()
    assert "port" in result


async def test_update_config_points_a_misplaced_key_at_its_real_section(tmp_path):
    """`thinking` is an [assistant] key and [assistant.generation] is the sub-table directly beneath it,
    so this is the miss to expect. The error has to carry the fix: the assistant retries from it alone."""
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant.generation", "thinking", "medium")

    assert "did you mean [assistant].thinking?" in result
    assert not path.exists()


async def test_update_config_lists_the_keys_a_known_section_accepts(tmp_path):
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant.generation", "warmth", "0.7")

    assert "Accepted in [assistant.generation]: context_length" in result
    assert "temperature" in result
    assert not path.exists()


def _stub_model_resolution(monkeypatch):
    """Let any model string resolve, so a test about something else does not depend on which provider
    extras are installed. `update_config` validates `[assistant].model` by building a throwaway client."""
    from aimu import aio

    monkeypatch.setattr(aio, "client", lambda model, system=None: object())


async def test_update_config_refuses_a_model_string_this_process_cannot_build(tmp_path):
    """`[assistant].model` is startup-only, so a bad value is not caught by a failed hot apply: without
    this check it persists and the failure surfaces at the next startup, with Kokua unable to start."""
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant", "model", "bogus-provider:whatever")

    assert result.startswith("Rejected:") and "bogus-provider" in result
    assert not path.exists()


async def test_update_config_writes_a_model_string_that_resolves(tmp_path, monkeypatch):
    _stub_model_resolution(monkeypatch)
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant", "model", "ollama:qwen3.8:27b")

    assert _read(path)["assistant"]["model"] == "ollama:qwen3.8:27b"
    assert "takes effect the next time Kokua restarts" in result


@pytest.mark.parametrize("key,value", [("model", "ollama:qwen3.8:27b"), ("thinking", "medium")])
async def test_update_config_says_a_startup_only_assistant_key_waits_for_a_restart(tmp_path, monkeypatch, key, value):
    """Neither is rebindable live -- no client is ever pointed at another model, and an agent's reasoning
    effort is fixed when it is built -- so the tool must not let the assistant report either as in force."""
    _stub_model_resolution(monkeypatch)
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant", key, value)

    assert _read(path)["assistant"][key] == value
    assert "takes effect the next time Kokua restarts" in result
