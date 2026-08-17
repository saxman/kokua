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
    result = await update_config("generation", "temperature", "0.3")
    assert _read(path)["generation"]["temperature"] == 0.3
    assert applied == [("generation", "temperature", 0.3)]
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
        raise RuntimeError("bad model")

    path, _, update_config = _tools(tmp_path, apply_hot)
    result = await update_config("assistant", "model", "nonsense:model")
    assert not path.exists()  # apply failed, so nothing was written
    assert "could not be applied" in result.lower()


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
