"""AssistantConfig: the derived paths and defaults."""

from __future__ import annotations


from kokua.cli import build_arg_parser, resolve_config
from kokua.config import AssistantConfig


def test_sessions_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path, memory=False)
    assert cfg.sessions_path == tmp_path / "sessions.json"


def test_logs_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path, memory=False)
    assert cfg.logs_path == tmp_path / "logs"


def test_default_confirm_tools():
    expected = ["add_skill_script", "add_mcp_server", "execute_python", "update_config"]
    assert AssistantConfig().confirm_tools == expected
    assert resolve_config(build_arg_parser().parse_args([])).confirm_tools == expected
