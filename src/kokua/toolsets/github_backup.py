"""Back up Kokua's own state to a private GitHub repository, as a git commit.

Contributes one tool, ``backup_kokua_state``, that mirrors an allowlist of ``$KOKUA_HOME`` paths into a
git working tree under the data directory, commits what changed, and pushes. It refuses a repository
GitHub does not report as private, makes no commit when nothing changed, and never force-pushes: a
diverged remote is reported for the user to reconcile by hand, since a mirror that can overwrite remote
history is not a backup.

The tool takes **no arguments**, and that is the whole safety argument. The repository, the branch, and
the files copied all come from ``config.toml``, so the model cannot redirect the capability and there is
nothing a per-call approval would protect. That is what earns it a place outside
``[security].confirm_tools``, and being outside that list is what lets it run at all in the proactive
turn a scheduled task fires, where gated tools auto-deny. A version of this tool taking a ``repo`` or a
``path`` argument would have to be gated, and a gated backup tool cannot be scheduled.

The token's environment variable name is fixed here rather than configurable. ``[github_backup].repo``
is this capability's blast radius and ``update_config`` is a tool the assistant holds, but a toolset
``Setting`` has no way to declare itself hand-edit-only. Fixing the variable name means a repointed
``repo`` can still only reach a repository that one token already writes, and the documentation says to
scope that token to the backup repository alone.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from aimu.tools import tool

from kokua.config import AssistantConfig
from kokua.toolsets.registry import Setting, Toolset

TOOLSET_NAME = "github_backup"
TOKEN_ENV = "GITHUB_BACKUP_TOKEN"
DEFAULT_BRANCH = "main"

GIT_TIMEOUT_SECONDS = 300
API_TIMEOUT_SECONDS = 30
COMMIT_NAME = "Kokua"
COMMIT_EMAIL = "kokua@localhost"

# The helper names the environment variable rather than carrying its value, so the token appears in no
# argv (which `ps` shows to every process of this user) and never lands in .git/config. The empty first
# value clears any inherited system or global helper (the macOS keychain, typically), which would
# otherwise be consulted first and could answer with a stale credential. It runs through a shell, so
# this works on macOS and Linux, and on Windows wherever git-bash is present.
_CREDENTIAL_HELPER = f'!f() {{ echo username=x-access-token; echo "password=${TOKEN_ENV}"; }}; f'


class BackupError(RuntimeError):
    """A backup could not complete. The message is written to be read by the user, not parsed."""


# Every status urllib.request.HTTPRedirectHandler would otherwise follow transparently.
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect instead of replaying credentials at whatever host it names.

    The default redirect handling resends every header from the original request, including the bearer
    token in ``Authorization``, to wherever the response's ``Location`` points, with no check that the
    destination is still api.github.com. That is the class of header-replay bug CVE-2023-32681 fixed in
    ``requests``. Refusing outright is simpler than following and then checking the new host, and it
    also produces the better message for the case this realistically hits: GitHub answers a renamed
    repository with a redirect, and the user needs telling that ``[github_backup].repo`` wants updating,
    not to have the redirect followed silently.

    Raising here, rather than returning ``None`` (which would hand the caller the redirect response
    body as though it were the answer), is what turns the refusal into the same ``HTTPError`` shape
    :func:`verify_repo_private` already branches on by status code.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def _validate_repo(repo: str) -> None:
    """Raise unless ``repo`` is a plausible ``owner/name`` pair, before it reaches a URL or a header.

    ``[github_backup].repo`` is not merely something a human typed once: ``update_config`` is a tool the
    assistant holds, so a value containing a newline or other control character can arrive here from a
    model turn. Unvalidated, that reaches ``http.client``'s CRLF-injection guard as a raw ``ValueError``
    instead of the ``BackupError`` every other failure in this function raises, breaking the contract
    ``backup_kokua_state`` relies on to turn any failure into a sentence rather than a traceback.
    """
    parts = repo.split("/")
    shape_is_valid = len(parts) == 2 and all(parts)
    characters_are_clean = all(character.isprintable() and not character.isspace() for character in repo)
    if not (shape_is_valid and characters_are_clean):
        raise BackupError(f"[github_backup].repo is {repo!r}, not a valid 'owner/name' repository")


def verify_repo_private(repo: str, token: str) -> None:
    """Raise unless ``repo`` exists, is reachable with ``token``, and is confirmed private.

    The one network call in an otherwise subprocess-only module, and it is worth it: what goes into this
    repository is the memory store, saved documents, and every conversation transcript, so pushing to a
    public repository has to be impossible rather than merely discouraged. That is also why every
    ambiguous outcome fails closed as a refusal instead of being read charitably: a redirect is refused
    rather than followed, a response that never finishes reading (truncated, timed out, or simply not
    valid JSON) is a failure rather than a pass, and a ``private`` value that is anything other than the
    literal boolean ``True`` (a missing key, ``null``, or a truthy string like ``"false"``) is treated as
    not private.

    Stdlib rather than ``httpx`` or ``requests``, both of which are only transitively present here, since
    this is a single GET.
    """
    _validate_repo(repo)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in _REDIRECT_CODES:
            raise BackupError(
                f"GitHub redirected {repo!r} instead of answering (HTTP {error.code}). The repository "
                "may have been renamed; check that [github_backup].repo still matches"
            ) from error
        if error.code in (403, 404):
            raise BackupError(
                f"repository {repo!r} was not found. Either it does not exist, or ${TOKEN_ENV} does not "
                "grant access to it (GitHub answers the same way for both)"
            ) from error
        if error.code == 401:
            raise BackupError(f"GitHub rejected ${TOKEN_ENV}") from error
        raise BackupError(f"GitHub returned HTTP {error.code} for {repo!r}") from error
    except urllib.error.URLError as error:
        raise BackupError(f"could not reach api.github.com: {error.reason}") from error
    except (json.JSONDecodeError, UnicodeDecodeError, http.client.HTTPException, TimeoutError) as error:
        # http.client.HTTPException covers a body cut short against its own Content-Length
        # (IncompleteRead), and TimeoutError covers one that stalls past API_TIMEOUT_SECONDS: neither is
        # a JSONDecodeError, but both are the same kind of failure this clause exists for, a read that
        # never produced a usable body, so the message says "read" rather than naming JSON specifically.
        raise BackupError(f"GitHub's response for {repo!r} could not be read: {error}") from error
    if not isinstance(payload, dict) or payload.get("private") is not True:
        raise BackupError(
            f"repository {repo!r} is not confirmed private. Kokua backs up your memory, documents and "
            "conversation transcripts, so it pushes only to a repository GitHub reports as private"
        )


def settings_for(config: AssistantConfig) -> tuple[str, str]:
    """This toolset's ``(repo, branch)``, with the branch falling back when the key is blank."""
    section = config.toolset_settings.get(TOOLSET_NAME, {})
    return section.get("repo", ""), section.get("branch") or DEFAULT_BRANCH


def backup_paths(config: AssistantConfig) -> list[tuple[Path, str]]:
    """Each source path and where it lands in the repository.

    An explicit allowlist rather than a walk of the state directory, which is what keeps the exclusions
    (rotating logs, binary downloads and images, the working tree itself) a consequence of this list
    instead of a second list that could drift from it.

    Addressed through ``AssistantConfig`` properties so a ``[paths] data_dir`` override moves all of it
    at once. The repository layout mirrors ``$KOKUA_HOME``, so restoring is copying these back with
    nothing to interpret.
    """
    return [
        (config.config_path, "config.toml"),
        (config.sessions_path, "data/sessions.json"),
        (config.memory_path, "data/memory"),
        (config.documents_path, "data/documents"),
        (config.skills_dir, "data/skills"),
    ]


def mirror_state(config: AssistantConfig, tree: Path) -> None:
    """Copy the allowlist into ``tree``, replacing each destination rather than merging into it.

    Replacing is what makes a deleted source file disappear from the backup; merging would leave it in
    the repository forever, and a copy that only ever grows is not a copy. Only the destinations named
    in :func:`backup_paths` are touched, so a ``README.md`` or a ``.gitignore`` the user added at the
    repository root survives every run (and the ``.gitignore`` is how anything further is excluded,
    since ``git add -A`` honours it).
    """
    for source, relative in backup_paths(config):
        destination = tree / relative
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def remote_url(repo: str) -> str:
    """The HTTPS remote for ``owner/name``.

    A named function rather than an inline f-string because it is the seam the test suite replaces with
    a local path, which is what lets the git plumbing be exercised for real without a network.
    """
    return f"https://github.com/{repo}.git"


def _credential_args() -> list[str]:
    return ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]


def _error_detail(stderr: str, stdout: str) -> str:
    """The one line from git's own output most likely to tell a user what went wrong.

    Git puts its own diagnosis first (an ``error:``, ``fatal:``, or `` ! [rejected]`` line) and any
    ``hint:`` advice that explains it last. Taking the last line, as a naive tail would, hands the user
    the hint instead of the error it is a footnote to.
    """
    lines = [line for line in (stderr or stdout).strip().splitlines() if line.strip()]
    for line in lines:
        if line.lstrip().startswith(("error:", "fatal:", "!")):
            return line.strip()
    return lines[-1].strip() if lines else "no output"


def _git(tree: Path, *args: str, label: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``tree``, raising :class:`BackupError` with git's own message on failure.

    ``GIT_TERMINAL_PROMPT=0`` is what keeps an unattended backup from hanging: without it, a credential
    git cannot satisfy makes it block on an interactive prompt that nobody is there to answer, and the
    scheduled turn never ends. Failing is the required behavior; hanging is not.

    ``label`` names the step for the error message, because the first argument is often ``-c`` and
    "git -c failed" tells a reader nothing.

    ``LC_ALL=C`` (with ``LANGUAGE`` cleared, since gettext lets it override ``LC_ALL``) pins git's
    output to the untranslated message set. Both :func:`_error_detail` and the empty-remote check in
    :func:`ensure_clone` parse that output by matching English substrings; on a git built with NLS
    (most Linux distributions ship one), an unpinned locale would translate those messages, the matches
    would silently stop firing, and the failure they exist to catch would go uncaught again.
    """
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANGUAGE": ""}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=tree,
            env=environment,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        # subprocess.run raises the same exception whether ``git`` is missing or ``cwd`` is: without this
        # check, a deleted working tree would be misreported as an absent git binary.
        if not tree.is_dir():
            raise BackupError(f"the backup working tree {tree} does not exist") from error
        raise BackupError("git is not installed, or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise BackupError(f"git {label} timed out after {GIT_TIMEOUT_SECONDS} seconds") from error
    if check and result.returncode != 0:
        raise BackupError(f"git {label} failed: {_error_detail(result.stderr, result.stdout)}")
    return result


def ensure_clone(tree: Path, repo: str, branch: str) -> None:
    """Make ``tree`` a working tree tracking ``repo``, in sync with the remote branch. Idempotent.

    The fetch-and-reset is what keeps the first push to an already-populated repository a fast-forward.
    On an empty repository the fetch fails harmlessly and the branch stays unborn until the first
    commit creates it.

    ``git init`` plus ``git checkout -b`` rather than ``git init -b``, which needs git 2.28 or newer.
    """
    if (tree / ".git").is_dir():
        return
    tree.mkdir(parents=True, exist_ok=True)
    _git(tree, "init", label="init")
    _git(tree, "checkout", "-b", branch, label="checkout", check=False)
    _git(tree, "remote", "add", "origin", remote_url(repo), label="remote add")
    fetched = _git(tree, *_credential_args(), "fetch", "--depth", "1", "origin", branch, label="fetch", check=False)
    if fetched.returncode == 0:
        _git(tree, "reset", "--hard", "FETCH_HEAD", label="reset")
    elif "couldn't find remote ref" not in fetched.stderr:
        # "couldn't find remote ref" is genuinely an empty remote and is fine to continue past with an
        # unborn branch. Anything else (a network error, a bad token) must not be read that way. .git is
        # already created by this point, so a swallowed failure here would not merely delay a retry: this
        # function returns early on every later call, so the tree would keep looking cloned while never
        # having actually synced. Raising here is what turns that silent wedge into a loud first-run
        # failure instead of a backup that quietly never reaches the remote.
        raise BackupError(f"git fetch failed: {_error_detail(fetched.stderr, fetched.stdout)}")


def head_sha(tree: Path) -> str:
    """The short SHA at HEAD, or an empty string when the branch is still unborn."""
    result = _git(tree, "rev-parse", "--short", "HEAD", label="rev-parse", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def commit_and_push(tree: Path, branch: str) -> tuple[str, int] | None:
    """Stage, commit, and push. Returns ``(short SHA, files changed)``, or ``None`` if nothing changed.

    The empty-diff check earns its place: a daily task would otherwise write an empty commit every day,
    and a history where every entry is identical cannot answer the one question it exists for, which is
    when something actually changed.

    Never ``--force``. A rejected push means the remote diverged, and reconciling that is the user's
    call: a mirror that can overwrite remote history is not a backup.
    """
    # core.excludesFile is cleared the same way the credential helper is: an inherited global (a stray
    # `*.sqlite3` in the developer's own ignore file, say) would otherwise silently drop a file from the
    # backup, reporting success with a changed-file count that looks entirely plausible. The in-tree
    # .gitignore at the repository root is untouched and still excludes whatever the user puts there.
    _git(tree, "-c", "core.excludesFile=", "add", "-A", label="add")
    staged = _git(tree, "diff", "--cached", "--name-only", label="diff")
    changed = [line for line in staged.stdout.splitlines() if line.strip()]
    if not changed:
        return None
    stamp = datetime.now().isoformat(timespec="seconds")
    _git(
        tree,
        "-c",
        f"user.name={COMMIT_NAME}",
        "-c",
        f"user.email={COMMIT_EMAIL}",
        # An inherited commit.gpgsign=true would otherwise fail every commit with no tty to answer the
        # signing prompt: the machine needs no git configuration of its own, for identity or for this.
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        f"Kokua backup {stamp}",
        label="commit",
    )
    _git(tree, *_credential_args(), "push", "origin", f"HEAD:{branch}", label="push")
    return head_sha(tree), len(changed)


def run_backup(config: AssistantConfig, *, verify: Callable[[str, str], None] = verify_repo_private) -> str:
    """Run one backup and return the sentence describing what happened.

    Every refusal is raised as :class:`BackupError` rather than returned, so the caller decides how to
    present it. :func:`build`'s tool turns it into text, because a tool that raises breaks the agent's
    tool loop and a scheduled turn would then have nothing to report.

    ``repo`` is validated here directly, ahead of the token lookup, rather than left to ``verify`` to
    discover: ``verify`` is the seam the test suite (and a future caller) can replace with a stub, and a
    stub that approves everything would otherwise let a malformed value reach :func:`remote_url` and a
    live git command unvetted.

    Ordered so that nothing is written before the repository has been vetted: a refused backup must
    leave no working tree behind for the next run to inherit.
    """
    repo, branch = settings_for(config)
    if not repo:
        raise BackupError("no repository is configured. Set [github_backup].repo in config.toml")
    _validate_repo(repo)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise BackupError(
            f"the ${TOKEN_ENV} environment variable is not set. It needs a GitHub token with "
            "`contents: write` on the backup repository"
        )
    verify(repo, token)

    tree = config.data_dir / "backup"
    try:
        ensure_clone(tree, repo, branch)
        mirror_state(config, tree)
    except OSError as error:
        # Everything past this point already raises BackupError (_git does its own catching), but
        # ensure_clone's tree.mkdir and mirror_state's copytree/rmtree/copy2 do not: mkdir can collide
        # with a stray file at `tree` or a read-only data_dir, and mirror_state copies out of a live
        # Chroma directory, where a WAL or temp file can vanish between the copytree scan and the copy
        # (shutil.Error, itself an OSError, is copytree's own way of reporting that). Left uncaught,
        # either would escape backup_kokua_state, whose only handler is for BackupError, and break the
        # agent's tool loop in exactly the unattended, scheduled turn that has no one to see a traceback.
        raise BackupError(f"could not prepare the backup working tree: {error}") from error
    committed = commit_and_push(tree, branch)
    if committed is None:
        previous = head_sha(tree) or "no commits yet"
        return f"Nothing to back up: Kokua's state is unchanged since the last backup ({previous})."
    sha, changed = committed
    plural = "" if changed == 1 else "s"
    return f"Backed up to {repo}@{branch}: {changed} file{plural} changed, commit {sha}."


def build(config: AssistantConfig, *, verify: Callable[[str, str], None] = verify_repo_private) -> list:
    """Return this toolset's one tool when a repository is configured, else nothing.

    Gated on ``repo`` for the reason ``image`` gates on its model env var: a default install has nowhere
    to push, so the model is never shown an option it cannot fulfill.

    ``verify`` is injected so the suite can exercise the whole path without reaching api.github.com.
    """
    repo, _ = settings_for(config)
    if not repo:
        return []

    @tool
    def backup_kokua_state() -> str:
        """Back up Kokua's own state to the user's private GitHub repository.

        Copies the configuration file, the memory store, saved documents, authored skills, and the
        conversation transcripts into a private repository as a git commit. Takes no arguments: the
        repository, the branch, and the files copied are all fixed by the user's configuration, and
        nothing here can be redirected. Makes no commit when nothing has changed since the last backup.
        """
        try:
            return run_backup(config, verify=verify)
        except BackupError as error:
            return f"Backup failed: {error}."

    return [backup_kokua_state]


GUIDANCE = (
    " You can back up Kokua's own state (its configuration, memory, documents, skills, and conversation "
    "transcripts) to the user's private GitHub repository by calling `backup_kokua_state`. It takes no "
    "arguments; the repository and the files copied are fixed by the user's configuration."
)

TOOLSET = Toolset(
    name=TOOLSET_NAME,
    description="Back up Kokua's own state to a private GitHub repository as a git commit.",
    build=lambda ctx: build(ctx.config),
    guidance=GUIDANCE,
    settings=(
        Setting("repo", str, ""),
        Setting("branch", str, DEFAULT_BRANCH),
    ),
    # Backing up its own state is how the agent manages itself, not domain work, so holding this does
    # not make a lean supervisor read as tool-heavy to the delegation guidance.
    cross_cutting=True,
)
