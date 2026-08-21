"""The github_backup toolset: the configuration gate, the mirror, the git plumbing, and the tool.

Nothing here reaches the network. Git runs against a bare repository in tmp_path over a file:// URL,
and the GitHub visibility check is the injectable seam `build` takes, so the suite never calls
api.github.com.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kokua.config import AssistantConfig
from kokua.toolsets.github_backup import (
    TOOLSET,
    TOOLSET_NAME,
    BackupError,
    backup_paths,
    build,
    commit_and_push,
    ensure_clone,
    head_sha,
    mirror_state,
)

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


def _run_git(*args: str, cwd: Path | None = None) -> str:
    """Run git for test setup, failing loudly. Not the module's runner; tests must not depend on it."""
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _bare_remote(tmp_path: Path) -> Path:
    """An empty bare repository whose HEAD points at main, to stand in for the GitHub repo.

    `git symbolic-ref` rather than `git init -b`, which needs git 2.28 or newer.
    """
    bare = tmp_path / "remote.git"
    _run_git("init", "--bare", str(bare))
    _run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    return bare


def _point_at(monkeypatch, bare: Path) -> None:
    """Redirect the module's remote URL at a local path, so no test ever reaches github.com."""
    from kokua.toolsets import github_backup

    monkeypatch.setattr(github_backup, "remote_url", lambda repo: bare.as_uri())


@needs_git
def test_first_run_against_an_empty_remote_creates_the_branch_and_pushes(tmp_path, monkeypatch):
    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    tree = tmp_path / "data/backup"

    ensure_clone(tree, "you/kokua-backup", "main")
    mirror_state(config, tree)
    result = commit_and_push(tree, "main")

    assert result is not None
    sha, changed = result
    assert changed == 5  # config.toml, sessions.json, and one file in each of the three folders
    assert _run_git("rev-parse", "--short", "main", cwd=bare) == sha


@needs_git
def test_first_run_against_a_remote_with_commits_fast_forwards(tmp_path, monkeypatch):
    """Without the fetch-and-reset in ensure_clone this push would be rejected as non-fast-forward."""
    bare = _bare_remote(tmp_path)
    seeder = tmp_path / "seeder"
    _run_git("clone", bare.as_uri(), str(seeder))
    (seeder / "README.md").write_text("my backups", encoding="utf-8")
    _run_git("add", "-A", cwd=seeder)
    _run_git("-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first", cwd=seeder)
    _run_git("push", "origin", "HEAD:main", cwd=seeder)

    _point_at(monkeypatch, bare)
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    tree = tmp_path / "data/backup"

    ensure_clone(tree, "you/kokua-backup", "main")
    assert (tree / "README.md").is_file()  # the existing history came down
    mirror_state(config, tree)
    result = commit_and_push(tree, "main")

    assert result is not None
    assert _run_git("rev-parse", "--short", "main", cwd=bare) == result[0]


@needs_git
def test_a_second_run_with_no_changes_makes_no_commit(tmp_path, monkeypatch):
    """A daily task must not write 365 empty commits a year: the history has to mean something."""
    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    tree = tmp_path / "data/backup"
    ensure_clone(tree, "you/kokua-backup", "main")
    mirror_state(config, tree)
    first = commit_and_push(tree, "main")

    mirror_state(config, tree)
    second = commit_and_push(tree, "main")

    assert second is None
    assert head_sha(tree) == first[0]


@needs_git
def test_a_deleted_source_file_reaches_the_remote_as_a_deletion(tmp_path, monkeypatch):
    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    tree = tmp_path / "data/backup"
    ensure_clone(tree, "you/kokua-backup", "main")
    mirror_state(config, tree)
    commit_and_push(tree, "main")

    (config.documents_path / "notes.md").unlink()
    mirror_state(config, tree)
    assert commit_and_push(tree, "main") is not None

    tracked = _run_git("ls-tree", "-r", "--name-only", "main", cwd=bare).splitlines()
    assert "data/documents/notes.md" not in tracked


@needs_git
def test_a_diverged_remote_is_reported_and_never_forced(tmp_path, monkeypatch):
    """A mirror that can overwrite remote history is not a backup. Reconciling is the user's call."""
    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    tree = tmp_path / "data/backup"
    ensure_clone(tree, "you/kokua-backup", "main")
    mirror_state(config, tree)
    commit_and_push(tree, "main")

    # Someone commits to the repository from elsewhere, then rewrites what we pushed.
    other = tmp_path / "other"
    _run_git("clone", bare.as_uri(), str(other))
    (other / "elsewhere.txt").write_text("x", encoding="utf-8")
    _run_git("add", "-A", cwd=other)
    _run_git("-c", "user.name=T", "-c", "user.email=t@e", "commit", "--amend", "-m", "rewritten", cwd=other)
    _run_git("push", "--force", "origin", "HEAD:main", cwd=other)

    (config.documents_path / "new.md").write_text("new", encoding="utf-8")
    mirror_state(config, tree)
    with pytest.raises(BackupError) as raised:
        commit_and_push(tree, "main")
    assert "push" in str(raised.value)


@needs_git
def test_head_sha_is_empty_before_the_first_commit(tmp_path, monkeypatch):
    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    tree = tmp_path / "data/backup"

    ensure_clone(tree, "you/kokua-backup", "main")

    assert head_sha(tree) == ""
