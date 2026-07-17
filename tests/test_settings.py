"""Tests for the TOML config file, precedence (CLI > file > default), and the data/ migration."""

from __future__ import annotations

import logging

import pytest

import kokua.paths as paths
from kokua import settings
from kokua.cli import _init_config, build_arg_parser, resolve_config


def _write_config(text: str):
    """Write config.toml at the default location ($KOKUA_HOME/config.toml) and return its path."""
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
        [display]
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


def test_logging_level_parses_from_file():
    _write_config(
        """
        [logging]
        level = "DEBUG"
        """
    )
    assert _resolve().log_level == "DEBUG"


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


def test_unknown_key_raises():
    _write_config('[assistant]\nbogus = 1\nmodel = "m"\n')
    with pytest.raises(settings.ConfigError, match=r"unknown config key \[assistant\].bogus"):
        settings.load()


def test_type_mismatch_raises():
    _write_config('[web]\nport = "not-an-int"\n')
    with pytest.raises(settings.ConfigError, match=r"\[web\].port must be an integer"):
        settings.load()


def test_bool_rejected_for_numeric_field():
    _write_config("[web]\nport = true\n")
    with pytest.raises(settings.ConfigError, match=r"\[web\].port must be an integer"):
        settings.load()


def test_security_confirm_tools_from_file():
    _write_config('[security]\nconfirm_tools = ["add_skill_script"]\n')
    assert _resolve().confirm_tools == ["add_skill_script"]


def test_planning_flags_from_file():
    _write_config("[planning]\nplan_review = true\nresult_review = true\n")
    cfg = _resolve()
    assert cfg.plan_review is True and cfg.result_review is True


def test_generation_section_collects_into_dict():
    _write_config("[generation]\ntemperature = 0.3\nmax_tokens = 2048\n")
    assert _resolve().generation == {"temperature": 0.3, "max_tokens": 2048}


def test_generation_unknown_key_raises():
    _write_config("[generation]\nbogus = 1\ntemperature = 0.5\n")
    with pytest.raises(settings.ConfigError, match=r"unknown config key \[generation\].bogus"):
        settings.load()


def test_generation_type_mismatch_raises():
    _write_config('[generation]\ntemperature = "hot"\n')
    with pytest.raises(settings.ConfigError, match=r"\[generation\].temperature must be a number"):
        settings.load()


def test_data_dir_override_redirects_leaf_paths(tmp_path):
    target = tmp_path / "elsewhere"
    _write_config(f'[paths]\ndata_dir = "{target}"\n')
    cfg = _resolve()
    assert cfg.data_dir == target
    assert cfg.skills_dir == target / "skills"
    assert cfg.sessions_path == target / "sessions.json"


def _init(*argv):
    return _init_config(build_arg_parser().parse_args(["config", "init", *argv]))


def test_config_init_writes_to_default_location():
    assert not paths.config_path().exists()
    assert _init() == 0
    assert paths.config_path().read_text(encoding="utf-8") == settings.example_text()


def test_config_init_refuses_to_overwrite_without_force():
    _write_config("# pre-existing\n")
    assert _init() == 1
    assert paths.config_path().read_text(encoding="utf-8") == "# pre-existing\n"


def test_config_init_force_overwrites():
    _write_config("# pre-existing\n")
    assert _init("--force") == 0
    assert paths.config_path().read_text(encoding="utf-8") == settings.example_text()


def test_config_init_custom_path(tmp_path):
    target = tmp_path / "custom" / "config.toml"
    assert _init("--path", str(target)) == 0
    assert target.read_text(encoding="utf-8") == settings.example_text()


def test_subagents_section_parses_concurrent_and_roles(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[subagents]\n"
        "concurrent = false\n\n"
        "[subagents.roles.researcher]\n"
        'groups = ["web"]\n'
        'description = "Custom researcher."\n\n'
        "[subagents.roles.dba]\n"
        'groups = ["compute"]\n'
        'description = "Database helper."\n'
        'system_message = "You manage databases."\n',
        encoding="utf-8",
    )
    overrides = settings.load(str(path))
    assert overrides["subagents_concurrent"] is False
    roles = overrides["subagent_roles"]
    assert roles["researcher"] == {"groups": ["web"], "description": "Custom researcher."}
    assert roles["dba"]["system_message"] == "You manage databases."


def test_subagents_unknown_role_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[subagents.roles.bad]\ngroups = ["web"]\nbogus = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(settings.ConfigError, match="bogus"):
        settings.load(str(path))


def test_subagents_unknown_top_level_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[subagents]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="bogus"):
        settings.load(str(path))


def test_shipped_example_loads_cleanly(caplog):
    """The example's active keys must parse without unknown-key or type warnings/errors."""
    _init()
    with caplog.at_level(logging.WARNING):
        overrides = settings.load()
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)
    assert overrides  # the example leaves several keys active at their default
    cfg = _resolve()
    assert cfg.show_thinking is True
