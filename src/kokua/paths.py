"""App-owned state locations.

The reference example stored everything under ``aimu.paths.output``; a standalone app owns
its own directory instead. The state root defaults to ``~/.kokua`` and is overridable with the
``KOKUA_HOME`` environment variable. The root holds an optional ``config.toml`` and a single
``data/`` directory under which all transient and user-provided content lives::

    $KOKUA_HOME/
      config.toml          # optional; read if present
      data/
        history.json
        memory/
        documents/
        skills/
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Root for all of Kokua's state: ``$KOKUA_HOME`` if set, else ``~/.kokua``.

    Configurable only via the env var, since the config file lives inside this directory and so
    must be locatable before it is read. Not created here; callers create what they write.
    """
    env = os.environ.get("KOKUA_HOME")
    return Path(env).expanduser() if env else Path.home() / ".kokua"


def data_dir() -> Path:
    """Directory holding all transient and user-provided content."""
    return state_dir() / "data"


def config_path() -> Path:
    return state_dir() / "config.toml"


def skills_dir() -> Path:
    return data_dir() / "skills"


def history_path() -> Path:
    return data_dir() / "history.json"


def sessions_path() -> Path:
    return data_dir() / "sessions.json"


def memory_dir() -> Path:
    return data_dir() / "memory"


def documents_dir() -> Path:
    return data_dir() / "documents"
