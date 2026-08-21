"""The github_backup toolset: the configuration gate, the mirror, the git plumbing, and the tool.

Nothing here reaches the network. Git runs against a bare repository in tmp_path over a file:// URL,
and the GitHub visibility check is the injectable seam `build` takes, so the suite never calls
api.github.com.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kokua.config import AssistantConfig
from kokua.toolsets.github_backup import TOOLSET, TOOLSET_NAME, build

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")


def _config(tmp_path: Path, **settings) -> AssistantConfig:
    """A config whose state root is tmp_path, with this toolset's section filled in.

    Mirrors what startup seeding does: every declared default is present before `build` runs.
    """
    config = AssistantConfig(data_dir=tmp_path / "data", config_path=tmp_path / "config.toml")
    config.toolset_settings[TOOLSET_NAME] = {"repo": "", "branch": "main", **settings}
    return config


def test_no_tools_until_a_repository_is_configured(tmp_path):
    assert build(_config(tmp_path)) == []


def test_the_tool_is_offered_once_a_repository_is_configured(tmp_path):
    tools = build(_config(tmp_path, repo="you/kokua-backup"))
    assert [fn.__name__ for fn in tools] == ["backup_kokua_state"]


def test_the_toolset_declares_its_two_settings():
    declared = {setting.key: (setting.kind, setting.default, setting.hot) for setting in TOOLSET.settings}
    assert declared == {"repo": (str, "", False), "branch": (str, "main", False)}


def test_the_toolset_is_named_for_its_config_section():
    # A toolset's settings section is always its own name, so these cannot be allowed to drift apart.
    assert TOOLSET.name == TOOLSET_NAME
