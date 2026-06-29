"""Tests for the TOML config file, precedence (CLI > file > default), and the data/ migration."""

from __future__ import annotations

import logging

import pytest

import mopai.paths as paths
from mopai import settings
from mopai.cli import build_arg_parser, resolve_config


def _write_config(text: str):
    """Write config.toml at the default location ($MOPAI_HOME/config.toml) and return its path."""
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _resolve(*argv):
    return resolve_config(build_arg_parser().parse_args(list(argv)))


def test_missing_default_file_is_no_op():
    assert settings.load() == {}


def test_file_overrides_built_in_defaults():
    _write_config(
        """
        [assistant]
        model = "anthropic:claude-sonnet-4-6"
        show_thinking = false
        [tools]
        groups = ["fs", "misc"]
        [web]
        port = 9100
        """
    )
    cfg = _resolve()
    assert cfg.model == "anthropic:claude-sonnet-4-6"
    assert cfg.show_thinking is False
    assert cfg.tools == ["fs", "misc"]
    assert cfg.port == 9100


def test_cli_overrides_file():
    _write_config(
        """
        [assistant]
        model = "from-file"
        [web]
        port = 9100
        """
    )
    cfg = _resolve("--model", "from-cli", "--port", "8500")
    assert cfg.model == "from-cli"
    assert cfg.port == 8500


def test_explicit_config_flag(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text('[frontend]\nname = "web"\n', encoding="utf-8")
    cfg = _resolve("--config", str(path))
    assert cfg.frontend == "web"


def test_explicit_missing_file_errors(tmp_path):
    with pytest.raises(settings.ConfigError, match="not found"):
        settings.load(str(tmp_path / "nope.toml"))


def test_unknown_key_warns_and_is_ignored(caplog):
    _write_config('[assistant]\nbogus = 1\nmodel = "m"\n')
    with caplog.at_level(logging.WARNING):
        cfg = _resolve()
    assert cfg.model == "m"
    assert any("bogus" in rec.message for rec in caplog.records)


def test_type_mismatch_raises():
    _write_config('[web]\nport = "not-an-int"\n')
    with pytest.raises(settings.ConfigError, match=r"\[web\].port must be an integer"):
        settings.load()


def test_bool_rejected_for_numeric_field():
    _write_config("[web]\nport = true\n")
    with pytest.raises(settings.ConfigError, match=r"\[web\].port must be an integer"):
        settings.load()


def test_data_dir_override_redirects_leaf_paths(tmp_path):
    target = tmp_path / "elsewhere"
    _write_config(f'[paths]\ndata_dir = "{target}"\n')
    cfg = _resolve()
    assert cfg.data_dir == target
    assert cfg.skills_dir == target / "skills"
    assert cfg.history_path == str(target / "history.json")
