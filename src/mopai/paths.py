"""App-owned state locations.

The reference example stored everything under ``aimu.paths.output``; a standalone app owns
its own directory instead. All persistent state (authored skills, conversation history, and
memory) lives under :func:`state_dir`, which defaults to ``~/.mopai`` and is overridable with
the ``MOPAI_HOME`` environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Return the root directory for all of Mopai's persistent state.

    ``$MOPAI_HOME`` if set, else ``~/.mopai``. The directory is not created here; the stores
    and managers that write under it create their own subdirectories on demand.
    """
    env = os.environ.get("MOPAI_HOME")
    return Path(env).expanduser() if env else Path.home() / ".mopai"


def skills_dir() -> Path:
    return state_dir() / "skills"


def history_path() -> Path:
    return state_dir() / "history.json"


def memory_dir() -> Path:
    return state_dir() / "memory"


def documents_dir() -> Path:
    return state_dir() / "documents"
