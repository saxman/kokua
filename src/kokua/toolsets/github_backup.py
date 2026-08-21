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
