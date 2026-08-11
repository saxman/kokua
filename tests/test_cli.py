"""The CLI surface: argument parsing and the defaults < file < flags precedence."""

from __future__ import annotations

import asyncio

import pytest


from kokua.cli import build_arg_parser, resolve_config
from kokua.config import AssistantConfig, paths
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


def test_default_tools_groups():
    assert AssistantConfig().tools == ["web", "fs", "compute", "time", "misc"]
    assert resolve_config(build_arg_parser().parse_args([])).tools == ["web", "fs", "compute", "time", "misc"]


def test_tools_flag_parses_groups():
    assert resolve_config(build_arg_parser().parse_args(["--tools", "web, misc"])).tools == ["web", "misc"]
    assert resolve_config(build_arg_parser().parse_args(["--tools", "none"])).tools == ["none"]


def test_memory_flag_parses():
    assert resolve_config(build_arg_parser().parse_args([])).memory is True
    assert resolve_config(build_arg_parser().parse_args(["--no-memory"])).memory is False


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


def test_main_lists_tool_packs(monkeypatch, capsys):
    _run_main(monkeypatch, ["--list-tool-packs"])
    out = capsys.readouterr().out
    assert "example:" in out


def test_main_listing_does_not_build_an_assistant(monkeypatch, capsys):
    """A listing must not resolve a model, so it works before anything is configured."""

    def explode(*args, **kwargs):
        raise AssertionError("main() built a config/assistant for a plain listing")

    monkeypatch.setattr("kokua.cli.resolve_config", explode)
    _run_main(monkeypatch, ["--list-frontends"])
    assert "cli:" in capsys.readouterr().out


def test_main_reports_an_unknown_frontend_with_the_available_ones(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    with pytest.raises(KeyError) as caught:
        _run_main(monkeypatch, ["--frontend", "nope"])
    message = str(caught.value)
    assert "nope" in message and "cli" in message  # names the miss and lists the choices
