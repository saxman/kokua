"""The CLI surface: argument parsing and the defaults < file < flags precedence."""

from __future__ import annotations

import asyncio

import pytest


from kokua.cli import build_arg_parser, resolve_config
from kokua.config import paths
from tests.channels import _config


def test_arg_parser_defaults():
    args = build_arg_parser().parse_args([])
    assert args.model is None
    assert args.config is None
    assert args.frontend is None  # resolve_config falls back to the "cli" default


def test_default_config_lives_under_state_dir():
    cfg = resolve_config(build_arg_parser().parse_args([]))
    assert cfg.data_dir == paths.data_dir()
    assert cfg.frontend == "cli"


def test_arg_parser_overrides():
    args = build_arg_parser().parse_args(
        [
            "--model",
            "anthropic:claude-sonnet-4-6",
            "--frontend",
            "web",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
        ]
    )
    cfg = resolve_config(args)
    assert cfg.model == "anthropic:claude-sonnet-4-6"
    assert cfg.frontend == "web"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000


def test_system_flag_sets_the_global_system_message():
    """--system writes [assistant].system_message, the opener any agent declaring none of its own falls
    back to (tests/toolsets/test_guidance.py covers the other half of that hop)."""
    assert resolve_config(build_arg_parser().parse_args(["--system", "Be terse."])).system_message == "Be terse."


def test_confirm_tools_flag_parses():
    cfg = resolve_config(build_arg_parser().parse_args(["--confirm-tools", "add_skill_script, execute_python"]))
    assert cfg.confirm_tools == ["add_skill_script", "execute_python"]


def test_confirm_tools_flag_empty_disables():
    assert resolve_config(build_arg_parser().parse_args(["--confirm-tools", ""])).confirm_tools == []


def test_cli_frontend_reports_model_client_error(tmp_path, monkeypatch, capsys):
    from kokua.core.assistant import ModelClientError
    from kokua.frontends import cli as cli_frontend

    async def boom(*args, **kwargs):
        raise ModelClientError("no default could be resolved; set AIMU_LANGUAGE_MODEL")

    monkeypatch.setattr(cli_frontend.Assistant, "create", boom)
    args = build_arg_parser().parse_args([])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli_frontend.run(_config(tmp_path), args))
    assert exc.value.code == 1
    assert "no default could be resolved" in capsys.readouterr().err


# --- main() dispatch --------------------------------------------------------------------------


def _run_main(monkeypatch, argv: list[str]):
    monkeypatch.setattr("sys.argv", ["kokua", *argv])
    from kokua.cli import main

    return main()


def test_main_lists_frontends(monkeypatch, capsys):
    _run_main(monkeypatch, ["--list-frontends"])
    out = capsys.readouterr().out
    assert "cli:" in out and "web:" in out


def test_main_lists_every_provider_kind_of_toolset(monkeypatch, capsys):
    """The single namespace needs one discovery command, so this lists the whole registry rather than the
    plugin entry points: a list omitting the built-in groups and core capabilities would read as "web and
    scheduling are not available to you", which is the opposite of true. Asserted one name per provider
    kind so the flag cannot silently narrow back to plugins only."""
    path = paths.config_path()
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[[mcp.server]]\nurl = "https://broker/mcp"\nname = "stocks"\n',
        encoding="utf-8",
    )
    _run_main(monkeypatch, ["--list-toolsets"])
    out = capsys.readouterr().out

    assert "web:" in out  # an AIMU built-in group
    assert "scheduling:" in out  # a Kokua core capability
    assert "example:" in out  # an installed plugin toolset
    assert "stocks:" in out  # a server configured in [[mcp.server]]
    # Grouped, because a flat list of names would not tell a user where any of them comes from.
    for provider in ("AIMU capability:", "core subsystem:", "plugin:", "MCP server:"):
        assert provider in out, provider


def test_listing_frontends_does_not_read_the_config(monkeypatch, capsys):
    """Which front ends exist is a property of the install, not of config.toml, so this listing answers
    before anything is configured. (--list-toolsets deliberately differs: the registry it prints depends
    on the file, so it resolves the config first.)"""

    def explode(*args, **kwargs):
        raise AssertionError("main() built a config/assistant for a plain listing")

    monkeypatch.setattr("kokua.cli.resolve_config", explode)
    _run_main(monkeypatch, ["--list-frontends"])
    assert "cli:" in capsys.readouterr().out


def test_main_reports_an_unknown_frontend_with_the_available_ones(monkeypatch, tmp_path):
    import pytest

    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(settings.example_text(), encoding="utf-8")  # config is required
    with pytest.raises(KeyError) as caught:
        _run_main(monkeypatch, ["--frontend", "nope"])
    message = str(caught.value)
    assert "nope" in message and "cli" in message  # names the miss and lists the choices


def test_main_reports_a_missing_config_without_a_traceback(monkeypatch, capsys, tmp_path):
    """Starting with no config.toml is now the ordinary first-run mistake, so it has to read as an
    instruction rather than as a crash."""
    import pytest

    from kokua.config import paths

    paths.config_path().unlink()
    with pytest.raises(SystemExit) as caught:
        _run_main(monkeypatch, [])
    assert caught.value.code != 0
    err = capsys.readouterr().err
    assert "config file not found" in err
    assert "kokua config init" in err
    assert "Traceback" not in err


def test_main_reports_a_config_with_no_agents_without_a_traceback(monkeypatch, capsys):
    import pytest

    from kokua.config import paths

    # configure_logging runs before the front end and calls faulthandler.enable(), which needs a real
    # stderr file descriptor that pytest's capture does not provide. Logging is not what is under test.
    monkeypatch.setattr("kokua.cli.configure_logging", lambda config: None)
    paths.config_path().write_text('[assistant]\nagent = "assistant"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        _run_main(monkeypatch, ["--frontend", "cli"])
    assert caught.value.code != 0
    err = capsys.readouterr().err
    assert "[agents." in err and "Traceback" not in err
