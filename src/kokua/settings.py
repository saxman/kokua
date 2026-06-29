"""Read an optional TOML config file into a dict of ``AssistantConfig`` overrides.

This module only finds and parses the file. Precedence (CLI flag > file > built-in default) is the
CLI's concern: it overlays these overrides onto anything the user passed on the command line.

File lookup order (first specified location wins):
    1. an explicit path (``--config``)
    2. ``$KOKUA_CONFIG``
    3. ``$KOKUA_HOME/config.toml`` (the default)

A missing default-location file is a silent no-op; a missing *explicitly requested* file is an
error, so a typo in ``--config`` / ``$KOKUA_CONFIG`` fails loudly instead of silently ignoring the
intended settings.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import tomllib
from pathlib import Path
from typing import Any, Callable, Optional

from . import paths

logger = logging.getLogger(__name__)

EXAMPLE_FILENAME = "config.example.toml"


class ConfigError(Exception):
    """The config file exists but has a missing-required or wrong-typed value."""


def example_text() -> str:
    """The shipped example config: every key at its built-in default, all documented."""
    return importlib.resources.files(__package__).joinpath(EXAMPLE_FILENAME).read_text(encoding="utf-8")


def _str_list(section: str, key: str, value: list) -> list[str]:
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"[{section}].{key} must be a list of strings")
    return list(value)


# (section, key) -> (AssistantConfig field, accepted TOML types, human label, optional converter).
# `bool` is an int subclass, so it is rejected for numeric fields unless explicitly accepted.
_SCHEMA: dict[tuple[str, str], tuple[str, tuple[type, ...], str, Optional[Callable]]] = {
    ("assistant", "model"): ("model", (str,), "a string", None),
    ("assistant", "system_message"): ("system_message", (str,), "a string", None),
    ("assistant", "reminder_seconds"): ("reminder_seconds", (int, float), "a number", lambda s, k, v: float(v)),
    ("assistant", "reminder_text"): ("reminder_text", (str,), "a string", None),
    ("assistant", "show_thinking"): ("show_thinking", (bool,), "a boolean", None),
    ("assistant", "show_tools"): ("show_tools", (bool,), "a boolean", None),
    ("assistant", "memory"): ("memory", (bool,), "a boolean", None),
    ("assistant", "load_plugins"): ("load_plugins", (bool,), "a boolean", None),
    ("tools", "groups"): ("tools", (list,), "a list of strings", _str_list),
    ("mcp", "servers"): ("mcp_servers", (list,), "a list of strings", _str_list),
    ("mcp", "bearer"): ("mcp_bearer", (str,), "a string", None),
    ("security", "confirm_tools"): ("confirm_tools", (list,), "a list of strings", _str_list),
    ("paths", "data_dir"): ("data_dir", (str,), "a string path", lambda s, k, v: Path(v).expanduser()),
    ("frontend", "name"): ("frontend", (str,), "a string", None),
    ("web", "host"): ("host", (str,), "a string", None),
    ("web", "port"): ("port", (int,), "an integer", None),
}


def resolve_path(explicit: Optional[str]) -> tuple[Path, bool]:
    """Return the config-file path and whether the user explicitly requested it."""
    if explicit:
        return Path(explicit).expanduser(), True
    env = os.environ.get("KOKUA_CONFIG")
    if env:
        return Path(env).expanduser(), True
    return paths.config_path(), False


def load(explicit: Optional[str] = None) -> dict[str, Any]:
    """Parse the config file (if any) into a dict of ``AssistantConfig`` field overrides."""
    path, requested = resolve_path(explicit)
    if not path.exists():
        if requested:
            raise ConfigError(f"config file not found: {path}")
        return {}

    with path.open("rb") as file:
        data = tomllib.load(file)

    overrides: dict[str, Any] = {}
    for section, entries in data.items():
        if not isinstance(entries, dict):
            logger.warning("ignoring top-level config key %r (expected a [section] table)", section)
            continue
        for key, value in entries.items():
            spec = _SCHEMA.get((section, key))
            if spec is None:
                logger.warning("ignoring unknown config key [%s].%s", section, key)
                continue
            field, types, label, convert = spec
            rejected_bool = isinstance(value, bool) and bool not in types
            if rejected_bool or not isinstance(value, types):
                raise ConfigError(f"[{section}].{key} must be {label}, got {type(value).__name__}")
            overrides[field] = convert(section, key, value) if convert else value
    return overrides
