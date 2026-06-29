"""App-owned state locations.

The reference example stored everything under ``aimu.paths.output``; a standalone app owns
its own directory instead. The state root defaults to ``~/.mopai`` and is overridable with the
``MOPAI_HOME`` environment variable. The root holds an optional ``config.toml`` and a single
``data/`` directory under which all transient and user-provided content lives::

    $MOPAI_HOME/
      config.toml          # optional; read if present
      data/
        history.json
        memory/
        documents/
        skills/
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Content that older versions wrote directly under the state root, relative to it. Used only by the
# one-time migration into ``data/``.
_LEGACY_ENTRIES = ("history.json", "memory", "documents", "skills")


def state_dir() -> Path:
    """Root for all of Mopai's state: ``$MOPAI_HOME`` if set, else ``~/.mopai``.

    Configurable only via the env var, since the config file lives inside this directory and so
    must be locatable before it is read. Not created here; callers create what they write.
    """
    env = os.environ.get("MOPAI_HOME")
    return Path(env).expanduser() if env else Path.home() / ".mopai"


def data_dir() -> Path:
    """Directory holding all transient and user-provided content."""
    return state_dir() / "data"


def config_path() -> Path:
    return state_dir() / "config.toml"


def skills_dir() -> Path:
    return data_dir() / "skills"


def history_path() -> Path:
    return data_dir() / "history.json"


def memory_dir() -> Path:
    return data_dir() / "memory"


def documents_dir() -> Path:
    return data_dir() / "documents"


def migrate_legacy_layout(data: Path) -> None:
    """Move pre-``data/`` content from the state root into ``data`` once.

    Older versions wrote ``history.json`` / ``memory`` / ``documents`` / ``skills`` directly under
    the state root. If any of those exist there and ``data`` does not yet exist, create ``data`` and
    move them in, preserving the user's history and memory. No-op once ``data`` exists or there is
    nothing to move.
    """
    if data.exists():
        return
    root = data.parent
    legacy = [root / name for name in _LEGACY_ENTRIES if (root / name).exists()]
    if not legacy:
        return
    data.mkdir(parents=True, exist_ok=True)
    for source in legacy:
        shutil.move(str(source), str(data / source.name))
