"""Back up Kokua's own state to a private GitHub repository, as a git commit.

Contributes one tool, ``backup_kokua_state``, that mirrors an allowlist of ``$KOKUA_HOME`` paths into a
git working tree under the data directory, commits what changed, and pushes.

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

import shutil
from pathlib import Path
from typing import Callable

from aimu.tools import tool

from kokua.config import AssistantConfig
from kokua.toolsets.registry import Setting, Toolset

TOOLSET_NAME = "github_backup"
TOKEN_ENV = "GITHUB_BACKUP_TOKEN"
DEFAULT_BRANCH = "main"


class BackupError(RuntimeError):
    """A backup could not complete. The message is written to be read by the user, not parsed."""


def verify_repo_private(repo: str, token: str) -> None:
    """Raise unless ``repo`` exists, is reachable with ``token``, and is private."""
    raise NotImplementedError


def run_backup(config: AssistantConfig, *, verify: Callable[[str, str], None] = verify_repo_private) -> str:
    """Run one backup and return the sentence describing what happened."""
    raise NotImplementedError


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
