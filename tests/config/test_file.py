"""Tests for the TOML config file, precedence (CLI > file > default), and the data/ migration."""

from __future__ import annotations

import logging

import pytest

from kokua.config import paths
from kokua.config import file as settings
from kokua.cli import _init_config, build_arg_parser, resolve_config


def _write_config(text: str):
    """Write config.toml at the default location ($KOKUA_HOME/config.toml) and return its path."""
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _resolve(*argv):
    return resolve_config(build_arg_parser().parse_args(list(argv)))


def test_missing_default_file_is_an_error_naming_the_fix():
    """config.toml is required: the [agents.*] tables live only there and the assistant needs at least
    one, so starting without a file is not a state Kokua can be in."""
    paths.config_path().unlink()
    with pytest.raises(settings.ConfigError, match="kokua config init"):
        settings.load()


def test_file_overrides_built_in_defaults():
    _write_config(
        """
        [assistant]
        model = "anthropic:claude-sonnet-4-6"
        concurrent_tools = false
        [display]
        show_thinking = false
        [web]
        port = 9100
        """
    )
    cfg = _resolve()
    assert cfg.model == "anthropic:claude-sonnet-4-6"
    assert cfg.show_thinking is False
    assert cfg.concurrent_tools is False
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
    paths.config_path().unlink()
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


def test_agent_tools_must_be_a_string_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[agents.bad]\ntools = [1, 2]\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="tools"):
        settings.load(str(path))


def test_agent_delegates_to_must_be_a_string_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[agents.bad]\ndelegates_to = [1]\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="delegates_to"):
        settings.load(str(path))


def test_subagents_unknown_top_level_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[subagents]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="bogus"):
        settings.load(str(path))


def test_mcp_server_tables_parse(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[[mcp.server]]\n"
        'url = "https://api.githubcopilot.com/mcp/"\n'
        'name = "github"\n'
        'token_env = "GITHUB_MCP_TOKEN"\n\n'
        "[[mcp.server]]\n"
        'url = "https://plain/mcp"\n'
        'name = "plain"\n',
        encoding="utf-8",
    )
    servers = settings.load(str(path))["mcp_servers"]
    assert [(s.url, s.token_env) for s in servers] == [
        ("https://api.githubcopilot.com/mcp/", "GITHUB_MCP_TOKEN"),
        ("https://plain/mcp", None),
    ]


def test_mcp_server_name_parses(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[mcp.server]]\nurl = "https://broker/mcp"\nname = "stocks"\n',
        encoding="utf-8",
    )
    servers = settings.load(str(path))["mcp_servers"]
    assert (servers[0].url, servers[0].name) == ("https://broker/mcp", "stocks")


def test_mcp_server_missing_url_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\ntoken_env = "X"\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="url"):
        settings.load(str(path))


def test_mcp_server_unknown_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\nurl = "https://x/mcp"\nname = "x"\nbearer = "nope"\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="bearer"):
        settings.load(str(path))


def test_mcp_unknown_top_level_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[mcp]\nservers = ["https://x/mcp"]\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError, match=r"\[mcp\].servers"):
        settings.load(str(path))


def test_agent_cache_cap_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[assistant]\nagent_cache_cap = 3\n")
    from kokua.config import file as settings

    overrides = settings.load()
    assert overrides["agent_cache_cap"] == 3


def test_coerce_config_string_scalars():
    assert settings.coerce_config_string("assistant", "model", "anthropic:x") == "anthropic:x"
    assert settings.coerce_config_string("display", "show_thinking", "false") is False
    assert settings.coerce_config_string("display", "show_tools", "true") is True
    assert settings.coerce_config_string("web", "port", "9100") == 9100


def test_coerce_config_string_list():
    assert settings.coerce_config_string("security", "confirm_tools", "execute_python, update_config") == [
        "execute_python",
        "update_config",
    ]


def test_coerce_config_string_generation_range_checked():
    assert settings.coerce_config_string("generation", "temperature", "0.3") == 0.3
    with pytest.raises(settings.ConfigError, match="temperature"):
        settings.coerce_config_string("generation", "temperature", "9")  # above the 2.0 max


def test_coerce_config_string_bad_int_raises():
    with pytest.raises(settings.ConfigError, match=r"\[web\].port"):
        settings.coerce_config_string("web", "port", "not-an-int")


def test_coerce_config_string_unknown_key_raises():
    with pytest.raises(settings.ConfigError, match="unknown config key"):
        settings.coerce_config_string("assistant", "bogus", "x")


def test_coerce_config_string_rejects_structured_sections():
    with pytest.raises(settings.ConfigError, match="mcp"):
        settings.coerce_config_string("mcp", "server", "x")


def test_shipped_example_loads_cleanly(caplog):
    """The example's active keys must parse without unknown-key or type warnings/errors."""
    _init()
    with caplog.at_level(logging.WARNING):
        overrides = settings.load()
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)
    assert overrides  # the example leaves several keys active at their default
    cfg = _resolve()
    assert cfg.show_thinking is True


def test_explicit_missing_file_is_also_an_error(tmp_path):
    """--config PATH pointing at nothing was already an error; the default location now behaves the
    same way, so the two paths do not disagree about whether a config is optional."""
    with pytest.raises(settings.ConfigError, match="config file not found"):
        settings.load(str(tmp_path / "nope.toml"))


def test_a_config_defining_no_agents_still_parses(tmp_path):
    """The file layer stays dumb about it: zero agents is a valid TOML config, and refusing to run on
    one is Assistant.create's call (see tests/core/test_build.py)."""
    path = tmp_path / "config.toml"
    path.write_text("[assistant]\nconcurrent_tools = true\n", encoding="utf-8")
    assert settings.load(str(path)).get("agents") is None
