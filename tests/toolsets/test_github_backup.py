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
from kokua.toolsets.github_backup import TOOLSET, TOOLSET_NAME, backup_paths, build, mirror_state

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


def _seed_state(config: AssistantConfig) -> None:
    """Write one file into each allowlisted location, so a mirror has something to find."""
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text("[assistant]\n", encoding="utf-8")
    config.sessions_path.parent.mkdir(parents=True, exist_ok=True)
    config.sessions_path.write_text("{}", encoding="utf-8")
    for directory, name in (
        (config.memory_path, "chroma.sqlite3"),
        (config.documents_path, "notes.md"),
        (config.skills_dir, "dice-roller/SKILL.md"),
    ):
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("seed", encoding="utf-8")


def test_the_allowlist_mirrors_the_state_layout(tmp_path):
    config = _config(tmp_path)
    assert [relative for _, relative in backup_paths(config)] == [
        "config.toml",
        "data/sessions.json",
        "data/memory",
        "data/documents",
        "data/skills",
    ]


def test_mirror_copies_every_allowlisted_path(tmp_path):
    config = _config(tmp_path)
    _seed_state(config)
    tree = tmp_path / "tree"
    tree.mkdir()

    mirror_state(config, tree)

    assert (tree / "config.toml").read_text(encoding="utf-8") == "[assistant]\n"
    assert (tree / "data/sessions.json").read_text(encoding="utf-8") == "{}"
    assert (tree / "data/memory/chroma.sqlite3").is_file()
    assert (tree / "data/documents/notes.md").is_file()
    assert (tree / "data/skills/dice-roller/SKILL.md").is_file()


def test_mirror_copies_nothing_that_is_not_allowlisted(tmp_path):
    """logs/, downloads/ and images/ stay out: noisy, binary, or re-fetchable."""
    config = _config(tmp_path)
    _seed_state(config)
    for directory, name in (
        (config.logs_path, "kokua.log"),
        (config.downloads_path, "report.pdf"),
        (config.images_path, "cat.png"),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text("x", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()

    mirror_state(config, tree)

    assert not (tree / "data/logs").exists()
    assert not (tree / "data/downloads").exists()
    assert not (tree / "data/images").exists()


def test_mirror_removes_what_the_source_no_longer_has(tmp_path):
    """A deleted memory file has to disappear from the backup, or the backup is not a copy."""
    config = _config(tmp_path)
    _seed_state(config)
    tree = tmp_path / "tree"
    tree.mkdir()
    mirror_state(config, tree)
    (config.memory_path / "chroma.sqlite3").unlink()

    mirror_state(config, tree)

    assert not (tree / "data/memory/chroma.sqlite3").exists()


def test_mirror_leaves_unrelated_files_in_the_tree_alone(tmp_path):
    """Someone's README or .gitignore in the backup repository must survive a run."""
    config = _config(tmp_path)
    _seed_state(config)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "README.md").write_text("my backups", encoding="utf-8")
    (tree / ".gitignore").write_text("data/memory/*.sqlite3\n", encoding="utf-8")

    mirror_state(config, tree)

    assert (tree / "README.md").read_text(encoding="utf-8") == "my backups"
    assert (tree / ".gitignore").is_file()


def test_mirror_tolerates_a_source_that_does_not_exist_yet(tmp_path):
    """A fresh install has no documents folder; that is not an error, just nothing to copy."""
    config = _config(tmp_path)
    config.config_path.write_text("[assistant]\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()

    mirror_state(config, tree)

    assert (tree / "config.toml").is_file()
    assert not (tree / "data/documents").exists()
