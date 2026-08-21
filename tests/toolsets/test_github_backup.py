"""The github_backup toolset: the configuration gate, the mirror, the git plumbing, and the tool.

Nothing here reaches the network. Git runs against a bare repository in tmp_path over a file:// URL,
and the GitHub visibility check is the injectable seam `build` takes, so the suite never calls
api.github.com.
"""

from __future__ import annotations

import http.client
import io
import json
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kokua.config import AssistantConfig
from kokua.toolsets import github_backup
from kokua.toolsets.github_backup import (
    TOKEN_ENV,
    TOOLSET,
    TOOLSET_NAME,
    BackupError,
    _error_detail,
    _git,
    backup_paths,
    build,
    commit_and_push,
    ensure_clone,
    head_sha,
    mirror_state,
    run_backup,
    verify_repo_private,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch, tmp_path):
    """Isolate every test in this module from the developer's own git configuration.

    A global ``core.excludesFile`` that matches ``*.sqlite3``, or a global ``commit.gpgsign = true``,
    would silently change what these tests observe (a smaller changed-file count, or every
    commit-writing test failing outright) without the module under test being at fault. Pointing git at
    empty global and system config files is what makes the fixes for those two hazards, in the module
    itself, the only thing keeping this suite green.
    """
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Make it impossible, not just conventional, for a test in this module to reach github.com.

    ``remote_url`` is the one seam through which a real hostname could ever enter this module's git
    calls. Patching it to fail here, for every test rather than only the ones that call ``_point_at``,
    turns this module's opening claim that nothing here reaches the network into something the test
    runner enforces rather than something a future test could forget.
    """

    def _forbidden(repo: str) -> str:
        raise AssertionError(f"remote_url({repo!r}) was not redirected by _point_at before use")

    monkeypatch.setattr("kokua.toolsets.github_backup.remote_url", _forbidden)


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
    remote_sha_after_rewrite = _run_git("rev-parse", "--short", "main", cwd=bare)

    (config.documents_path / "new.md").write_text("new", encoding="utf-8")
    mirror_state(config, tree)
    with pytest.raises(BackupError) as raised:
        commit_and_push(tree, "main")
    # "push" alone is not evidence: it is the step label on every push failure, this one included, so an
    # auth error or a missing origin would pass just as well. The actual rejection text is what proves
    # this is the diverged-history case and not something else that happens to share the word "push".
    message = str(raised.value)
    assert "rejected" in message or "fetch first" in message
    assert _run_git("rev-parse", "--short", "main", cwd=bare) == remote_sha_after_rewrite


@needs_git
def test_head_sha_is_empty_before_the_first_commit(tmp_path, monkeypatch):
    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    tree = tmp_path / "data/backup"

    ensure_clone(tree, "you/kokua-backup", "main")

    assert head_sha(tree) == ""


def test_error_detail_prefers_the_error_line_over_a_trailing_hint():
    """Git's diagnosis comes first and its "hint:" footnotes come last; the tail is the wrong end to read."""
    stderr = (
        " ! [rejected]        HEAD -> main (fetch first)\n"
        "error: failed to push some refs to 'file:///tmp/remote.git'\n"
        "hint: Updates were rejected because the remote contains work that you do not\n"
        "hint: have locally. This is usually caused by another repository pushing to\n"
        "hint: the same ref. If you want to integrate the remote changes, use\n"
        "hint: 'git pull' before pushing again.\n"
        "hint: See the 'Note about fast-forwards' in 'git push --help' for details.\n"
    )
    assert _error_detail(stderr, "") == "! [rejected]        HEAD -> main (fetch first)"


def test_error_detail_falls_back_to_the_last_line_when_nothing_looks_like_an_error():
    assert _error_detail("just some output\nwith no error marker\n", "") == "with no error marker"


def test_error_detail_falls_back_to_stdout_when_stderr_is_empty():
    assert _error_detail("", "fatal: from stdout for some reason\n") == "fatal: from stdout for some reason"


@needs_git
def test_add_ignores_an_inherited_global_excludes_file(tmp_path, monkeypatch):
    """A developer's own ``*.sqlite3`` in a global git ignore must not silently drop the memory store.

    Without clearing ``core.excludesFile`` on the add, ``git add -A`` would honor this inherited global
    and the backup would report success while quietly omitting the memory database from the commit.
    """
    global_ignore = tmp_path / "global-gitignore"
    global_ignore.write_text("*.sqlite3\n", encoding="utf-8")
    global_config = tmp_path / "global-gitconfig-with-excludes"
    global_config.write_text(f"[core]\n\texcludesFile = {global_ignore}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    tree = tmp_path / "data/backup"

    ensure_clone(tree, "you/kokua-backup", "main")
    mirror_state(config, tree)
    result = commit_and_push(tree, "main")

    assert result is not None
    tracked = _run_git("ls-tree", "-r", "--name-only", "main", cwd=bare).splitlines()
    assert "data/memory/chroma.sqlite3" in tracked


@needs_git
def test_commit_succeeds_despite_an_inherited_global_gpgsign(tmp_path, monkeypatch):
    """An unattended backup must not fail because some global config expects a tty to sign with.

    Without setting ``commit.gpgsign=false`` alongside the identity, a global ``commit.gpgsign = true``
    would fail every commit this module writes, with no terminal there to answer a signing prompt.
    """
    global_config = tmp_path / "global-gitconfig-with-gpgsign"
    global_config.write_text(
        "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /nonexistent-gpg-binary\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    tree = tmp_path / "data/backup"

    ensure_clone(tree, "you/kokua-backup", "main")
    mirror_state(config, tree)
    result = commit_and_push(tree, "main")

    assert result is not None


@needs_git
def test_ensure_clone_raises_on_a_genuine_fetch_failure(tmp_path, monkeypatch):
    """An auth or network fetch failure must not be read as "the remote is empty".

    Swallowing every fetch failure that way would leave the branch unborn, and because ensure_clone
    returns early once .git exists, the tree would never retry: the next run's push would be rejected as
    non-fast-forward, or the run would report an empty diff and the user would be told the backup is
    fine when the remote was never reached at all.
    """
    from kokua.toolsets import github_backup

    monkeypatch.setattr(github_backup, "remote_url", lambda repo: (tmp_path / "does-not-exist.git").as_uri())
    tree = tmp_path / "data/backup"

    with pytest.raises(BackupError, match="fetch"):
        ensure_clone(tree, "you/kokua-backup", "main")


@needs_git
def test_ensure_clone_tolerates_a_genuinely_empty_remote(tmp_path, monkeypatch):
    """The one fetch failure that must not raise: an empty remote has no ref yet, by design."""
    bare = _bare_remote(tmp_path)
    _point_at(monkeypatch, bare)
    tree = tmp_path / "data/backup"

    ensure_clone(tree, "you/kokua-backup", "main")

    assert (tree / ".git").is_dir()


def test_git_pins_the_locale_so_message_matching_stays_untranslated(tmp_path, monkeypatch):
    """_error_detail and the empty-remote check in ensure_clone both match untranslated git output.

    A git built with NLS (most Linux distributions ship one) would translate "fatal:", "error:", and
    "couldn't find remote ref" under an inherited locale, and those matches would stop firing with
    nothing raised to say so. This asserts the pin directly (`_git` fakes `subprocess.run` and inspects
    the environment it was called with), since the git installed here carries no locale data and so
    cannot demonstrate the mistranslation the pin prevents.
    """
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _git(tmp_path, "status", label="status")

    assert captured["env"]["LC_ALL"] == "C"
    assert captured["env"]["LANGUAGE"] == ""


def _fake_response(body: bytes):
    """Stand in for the opener's `.open()`, returning `body` verbatim as the response."""

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exception):
            return False

    return lambda request, timeout=None: _Response(body)


def _fake_urlopen(payload: object):
    """Stand in for the opener's `.open()`, returning `payload` JSON-encoded as the response body."""
    return _fake_response(json.dumps(payload).encode("utf-8"))


def _fake_http_error(code: int):
    def raise_it(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "boom", {}, None)

    return raise_it


def _patch_open(monkeypatch, open_):
    """Route the module's one network call through `open_`, standing in for the opener it builds.

    `verify_repo_private` builds its own opener rather than calling `urlopen` directly, since installing
    the redirect refusal (`_RefuseRedirect`) needs `build_opener`. That makes `build_opener` the seam to
    patch: patching `urlopen` alone would leave a real, network-reaching opener answering the call.
    """

    class _Opener:
        def open(self, request, timeout=None):
            return open_(request, timeout)

    monkeypatch.setattr(github_backup.urllib.request, "build_opener", lambda *handlers: _Opener())


def test_a_private_repository_is_accepted(monkeypatch):
    _patch_open(monkeypatch, _fake_urlopen({"private": True}))
    assert verify_repo_private("you/kokua-backup", "token") is None


def test_a_public_repository_is_refused(monkeypatch):
    """Memory, documents and transcripts go into this repository. Public is never the right answer."""
    _patch_open(monkeypatch, _fake_urlopen({"private": False}))
    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert "private" in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"private": "false"},  # the sharpest case: reads as "not private" to a human, and `not "false"`
        # evaluates to `False`, so a bare truthiness test would have accepted it.
        {"private": "true"},
        {"private": 1},
        {"private": None},
        {},
    ],
)
def test_a_non_true_private_value_is_refused(monkeypatch, payload):
    """Only the literal boolean `True` counts as confirmation. Anything else fails closed."""
    _patch_open(monkeypatch, _fake_urlopen(payload))
    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert "private" in str(raised.value)


def test_the_refusal_message_does_not_assert_public_when_it_is_merely_unconfirmed(monkeypatch):
    """A missing or malformed `private` key means GitHub's answer was not a confirmation, not that the
    repository is known to be public; the message must not claim more than that."""
    _patch_open(monkeypatch, _fake_urlopen({}))
    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert "public" not in str(raised.value)


def test_an_unparseable_response_body_is_a_backup_error_not_a_traceback(monkeypatch):
    """`backup_kokua_state` catches only `BackupError`; a `json.JSONDecodeError` escaping this function
    would reach the model as a raw traceback instead of the intended "Backup failed: ..." sentence."""
    _patch_open(monkeypatch, _fake_response(b"not json"))
    with pytest.raises(BackupError):
        verify_repo_private("you/kokua-backup", "token")


def test_a_non_object_response_body_is_a_backup_error(monkeypatch):
    """A bare JSON array parses without error but has no `.get`, so this is a distinct failure mode
    from a body that fails to parse at all, and must be caught the same way."""
    _patch_open(monkeypatch, _fake_response(b"[1, 2, 3]"))
    with pytest.raises(BackupError):
        verify_repo_private("you/kokua-backup", "token")


def _truncated_server() -> int:
    """A local server that declares a Content-Length larger than the bytes it actually sends.

    Standing in for a connection that drops mid-response: reading past what the server actually sent
    raises `http.client.IncompleteRead` from inside `json.load`'s own read of the response, which is
    the shape this test exists to prove is caught rather than left to escape as a raw exception. A real
    socket, bound to loopback only, is what makes this a genuine `http.client.IncompleteRead` rather
    than a stand-in for one; the module never learns the difference between this and api.github.com.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        connection, _ = server.accept()
        try:
            connection.recv(4096)
            body = b'{"private": true}'
            # Content-Length promises 100 bytes more than `body` actually holds, then the connection
            # closes, which is what turns the short read into IncompleteRead instead of a clean EOF.
            header = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body) + 100}\r\nConnection: close\r\n\r\n"
            connection.sendall(header.encode("ascii") + body)
        finally:
            connection.close()
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def test_a_truncated_response_body_is_a_backup_error_not_a_traceback(monkeypatch):
    """`http.client.IncompleteRead` subclasses `HTTPException`, not `URLError`, so it escaped both of
    this function's original except clauses and would have reached the model as a raw traceback."""
    port = _truncated_server()

    def open_(request, timeout=None):
        # http.client.HTTPConnection rather than urllib.request.urlopen: build_opener is monkeypatched
        # module-globally by _patch_open, so a call to urlopen from inside this fake would recurse into
        # the very same patch instead of reaching a real opener.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        connection.request("GET", "/")
        return connection.getresponse()

    _patch_open(monkeypatch, open_)

    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert "could not be read" in str(raised.value)


def test_a_response_that_times_out_partway_through_is_a_backup_error(monkeypatch):
    """`TimeoutError` subclasses `OSError`, not `urllib.error.URLError`, so a read that stalls past
    `API_TIMEOUT_SECONDS` escaped this function's except clauses the same way a truncated one did."""

    def open_(request, timeout=None):
        raise TimeoutError("timed out")

    _patch_open(monkeypatch, open_)

    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert "could not be read" in str(raised.value)


@pytest.mark.parametrize("code", [403, 404])
def test_a_repository_the_token_cannot_see_is_refused(monkeypatch, code):
    """GitHub answers 404 for both "no such repo" and "your token cannot see it", so the message says so."""
    _patch_open(monkeypatch, _fake_http_error(code))
    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert "not found" in str(raised.value)
    assert TOKEN_ENV in str(raised.value)


def test_a_rejected_token_says_so(monkeypatch):
    _patch_open(monkeypatch, _fake_http_error(401))
    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert TOKEN_ENV in str(raised.value)


def test_the_request_carries_the_token_as_a_bearer_header(monkeypatch):
    seen = {}

    def capture(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _fake_urlopen({"private": True})(request, timeout)

    _patch_open(monkeypatch, capture)
    verify_repo_private("you/kokua-backup", "s3cret")

    assert seen["url"] == "https://api.github.com/repos/you/kokua-backup"
    assert seen["auth"] == "Bearer s3cret"


def test_the_redirect_handler_refuses_rather_than_following():
    """`redirect_request` is the exact seam `urlopen` calls before ever resending a header to the
    redirect target. Raising here, rather than the default of following, is what stops the
    `Authorization` bearer token from ever reaching a second host (the class of bug CVE-2023-32681
    fixed in `requests`). Exercised directly, with no opener involved, so this genuinely proves the
    handler itself refuses rather than the surrounding test double."""
    handler = github_backup._RefuseRedirect()
    request = urllib.request.Request("https://api.github.com/repos/you/kokua-backup")

    with pytest.raises(urllib.error.HTTPError) as raised:
        handler.redirect_request(
            request, None, 301, "Moved Permanently", {}, "https://api.github.com/repositories/12345"
        )

    assert raised.value.code == 301


def test_verify_repo_private_builds_its_opener_with_the_redirect_refusal(monkeypatch):
    """The unit test above proves the handler refuses; this proves `verify_repo_private` actually wires
    it into the opener it uses for the real call, rather than the two having drifted apart."""
    captured = {}

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return type("_Opener", (), {"open": staticmethod(_fake_urlopen({"private": True}))})()

    monkeypatch.setattr(github_backup.urllib.request, "build_opener", fake_build_opener)

    verify_repo_private("you/kokua-backup", "token")

    assert github_backup._RefuseRedirect in captured["handlers"]


def test_a_redirected_repository_is_refused_with_a_rename_hint(monkeypatch):
    """The realistic trigger for a redirect here is GitHub renaming the repository; the message should
    say so rather than reporting a bare, unexplained HTTP failure."""
    _patch_open(monkeypatch, _fake_http_error(301))
    with pytest.raises(BackupError) as raised:
        verify_repo_private("you/kokua-backup", "token")
    assert "renamed" in str(raised.value)


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "missing-a-slash",
        "you/",
        "/kokua-backup",
        "you/kokua-backup/extra",
        "you/kokua-backup\nwith-a-newline",
    ],
)
def test_a_malformed_repo_is_refused_before_any_request_is_built(monkeypatch, repo):
    """`[github_backup].repo` is writable by the assistant through `update_config`, so a value with an
    embedded newline or other control character can arrive here from a model turn. Unvalidated, it would
    reach `http.client`'s CRLF-injection guard as a raw `ValueError` rather than a `BackupError`. Failing
    before any opener is built, rather than merely by the time the network call raises, is what this test
    checks: the assertion below would raise `AssertionError` if validation did not come first.
    """

    def _forbidden(*handlers):
        raise AssertionError("a malformed repo must be rejected before any opener is built")

    monkeypatch.setattr(github_backup.urllib.request, "build_opener", _forbidden)

    with pytest.raises(BackupError) as raised:
        verify_repo_private(repo, "token")
    assert "github_backup" in str(raised.value)


def _accepts_anything(repo: str, token: str) -> None:
    """A verify seam that approves, standing in for the api.github.com call."""


@needs_git
def test_a_backup_reports_the_repository_the_branch_and_the_commit(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "token")
    _point_at(monkeypatch, _bare_remote(tmp_path))
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)

    message = run_backup(config, verify=_accepts_anything)

    assert message.startswith("Backed up to you/kokua-backup@main:")
    assert "5 file" in message


@needs_git
def test_an_unchanged_state_reports_the_last_commit_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "token")
    _point_at(monkeypatch, _bare_remote(tmp_path))
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)
    run_backup(config, verify=_accepts_anything)

    message = run_backup(config, verify=_accepts_anything)

    assert message.startswith("Nothing to back up")
    assert head_sha(config.data_dir / "backup") in message


@needs_git
def test_the_working_tree_lives_under_the_data_directory(tmp_path, monkeypatch):
    """So a [paths] data_dir override moves it too, and it stays outside the allowlist."""
    monkeypatch.setenv(TOKEN_ENV, "token")
    _point_at(monkeypatch, _bare_remote(tmp_path))
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)

    run_backup(config, verify=_accepts_anything)

    assert (config.data_dir / "backup" / ".git").is_dir()


def test_a_missing_token_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    config = _config(tmp_path, repo="you/kokua-backup")
    with pytest.raises(BackupError) as raised:
        run_backup(config, verify=_accepts_anything)
    assert TOKEN_ENV in str(raised.value)


def test_a_missing_repository_is_refused_by_key(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "token")
    config = _config(tmp_path)
    with pytest.raises(BackupError) as raised:
        run_backup(config, verify=_accepts_anything)
    assert "[github_backup].repo" in str(raised.value)


def test_a_malformed_repository_is_refused_even_when_verify_approves_everything(tmp_path, monkeypatch):
    """`verify` is the seam every test (and any future caller) can replace with a stub. If run_backup
    relied on `verify` alone to validate `repo`, a stub that approves everything would let a malformed
    value reach `remote_url` and a live git command unchecked. `run_backup` must check it directly."""
    monkeypatch.setenv(TOKEN_ENV, "token")
    config = _config(tmp_path, repo="not-a-valid-repo-name")

    with pytest.raises(BackupError) as raised:
        run_backup(config, verify=_accepts_anything)

    assert "github_backup" in str(raised.value)
    assert not (config.data_dir / "backup").exists()


def test_the_verifier_runs_before_anything_is_written(tmp_path, monkeypatch):
    """A refused repository must leave no working tree behind, or the next run inherits half a clone."""
    monkeypatch.setenv(TOKEN_ENV, "token")
    config = _config(tmp_path, repo="you/kokua-backup")

    def refuse(repo, token):
        raise BackupError("repository 'you/kokua-backup' is public")

    with pytest.raises(BackupError):
        run_backup(config, verify=refuse)
    assert not (config.data_dir / "backup").exists()


def test_the_tool_returns_a_failure_as_text_rather_than_raising(tmp_path, monkeypatch):
    """A tool that raises breaks the agent's tool loop, so every failure comes back as a sentence."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    (backup_kokua_state,) = build(_config(tmp_path, repo="you/kokua-backup"), verify=_accepts_anything)

    result = backup_kokua_state()

    assert result.startswith("Backup failed:")
    assert TOKEN_ENV in result


def test_the_tool_takes_no_arguments():
    """Zero arguments is what makes this safe to run ungated, and so safe to schedule."""
    import inspect

    (backup_kokua_state,) = build(_config(Path("/nonexistent"), repo="you/kokua-backup"), verify=_accepts_anything)
    assert list(inspect.signature(backup_kokua_state).parameters) == []


@needs_git
def test_the_token_never_reaches_a_command_line(tmp_path, monkeypatch):
    """`ps` shows argv to every process of this user, so the token must only ever be in the environment."""
    from kokua.toolsets import github_backup

    monkeypatch.setenv(TOKEN_ENV, "s3cret-token-value")
    _point_at(monkeypatch, _bare_remote(tmp_path))
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)

    recorded: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        recorded.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr(github_backup.subprocess, "run", recording_run)
    run_backup(config, verify=_accepts_anything)

    assert recorded, "no git command ran, so this proves nothing"
    assert not any("s3cret-token-value" in argument for call in recorded for argument in call)
    # The helper carries the variable's name, which is how the value stays out of argv.
    assert any(TOKEN_ENV in argument for call in recorded for argument in call)


@needs_git
def test_no_git_command_ever_forces(tmp_path, monkeypatch):
    from kokua.toolsets import github_backup

    monkeypatch.setenv(TOKEN_ENV, "token")
    _point_at(monkeypatch, _bare_remote(tmp_path))
    config = _config(tmp_path, repo="you/kokua-backup")
    _seed_state(config)

    recorded: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        recorded.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr(github_backup.subprocess, "run", recording_run)
    run_backup(config, verify=_accepts_anything)

    forcing = [call for call in recorded if "--force" in call or "-f" in call]
    assert forcing == []
