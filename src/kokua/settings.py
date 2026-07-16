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
import os
import tomllib
from pathlib import Path
from typing import Any, Callable, Optional

from . import paths, runtime_settings

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


_SUBAGENT_ROLE_KEYS = {"description": str, "groups": list, "system_message": str}


def _parse_subagent_role(name: str, spec: Any) -> dict:
    """Validate one [subagents.roles.<name>] table into a role dict."""
    if not isinstance(spec, dict):
        raise ConfigError(f"[subagents.roles.{name}] must be a table")
    role: dict = {}
    for key, value in spec.items():
        expected = _SUBAGENT_ROLE_KEYS.get(key)
        if expected is None:
            raise ConfigError(f"unknown config key [subagents.roles.{name}].{key}")
        if key == "groups":
            role[key] = _str_list(f"subagents.roles.{name}", key, value)
        elif not isinstance(value, expected):
            raise ConfigError(f"[subagents.roles.{name}].{key} must be a {expected.__name__}")
        else:
            role[key] = value
    return role


# (section, key) -> (AssistantConfig field, accepted TOML types, human label, optional converter).
# `bool` is an int subclass, so it is rejected for numeric fields unless explicitly accepted.
_SCHEMA: dict[tuple[str, str], tuple[str, tuple[type, ...], str, Optional[Callable]]] = {
    ("assistant", "model"): ("model", (str,), "a string", None),
    ("assistant", "system_message"): ("system_message", (str,), "a string", None),
    ("display", "show_thinking"): ("show_thinking", (bool,), "a boolean", None),
    ("display", "show_tools"): ("show_tools", (bool,), "a boolean", None),
    ("planning", "plan_review"): ("plan_review", (bool,), "a boolean", None),
    ("planning", "plan_review_agent"): ("plan_review_agent", (bool,), "a boolean", None),
    ("planning", "result_review"): ("result_review", (bool,), "a boolean", None),
    ("planning", "review_rounds"): ("review_rounds", (int,), "an integer", None),
    ("planning", "show_reasoning"): ("show_reasoning", (bool,), "a boolean", None),
    ("assistant", "memory"): ("memory", (bool,), "a boolean", None),
    ("assistant", "load_plugins"): ("load_plugins", (bool,), "a boolean", None),
    ("assistant", "subagents"): ("subagents", (bool,), "a boolean", None),
    ("tools", "groups"): ("tools", (list,), "a list of strings", _str_list),
    ("mcp", "servers"): ("mcp_servers", (list,), "a list of strings", _str_list),
    ("mcp", "bearer"): ("mcp_bearer", (str,), "a string", None),
    # [email]: SMTP send settings. No `password` key on purpose -- the password comes from the
    # KOKUA_EMAIL_PASSWORD env var, so putting it here is a hard "unknown config key" error.
    ("email", "host"): ("email_host", (str,), "a string", None),
    ("email", "port"): ("email_port", (int,), "an integer", None),
    ("email", "username"): ("email_username", (str,), "a string", None),
    ("email", "from"): ("email_from", (str,), "a string", None),
    ("email", "to"): ("email_to", (str,), "a string", None),
    ("email", "use_ssl"): ("email_use_ssl", (bool,), "a boolean", None),
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
            raise ConfigError(f"top-level config key {section!r} is not a [section] table")
        # The [generation] table maps to the single `generation` dict field (one key per generation
        # kwarg) rather than the usual one-key-one-field _SCHEMA entries, so handle it separately.
        # Types are checked loudly here; range validation is left to runtime_settings.sanitize.
        if section == "generation":
            for key, value in entries.items():
                if key not in runtime_settings.GENERATION_KEYS:
                    raise ConfigError(f"unknown config key [generation].{key}")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ConfigError(f"[generation].{key} must be a number, got {type(value).__name__}")
                overrides.setdefault("generation", {})[key] = value
            continue
        # The [subagents] table maps to two fields: `concurrent` (bool) and a nested `roles` table of
        # role definitions, so it is handled specially like [generation] rather than via _SCHEMA.
        if section == "subagents":
            for key, value in entries.items():
                if key == "concurrent":
                    if not isinstance(value, bool):
                        raise ConfigError(f"[subagents].concurrent must be a boolean, got {type(value).__name__}")
                    overrides["subagents_concurrent"] = value
                elif key == "roles":
                    if not isinstance(value, dict):
                        raise ConfigError("[subagents.roles] must be a table of role definitions")
                    overrides["subagent_roles"] = {
                        name: _parse_subagent_role(name, spec) for name, spec in value.items()
                    }
                else:
                    raise ConfigError(f"unknown config key [subagents].{key}")
            continue
        for key, value in entries.items():
            spec = _SCHEMA.get((section, key))
            if spec is None:
                raise ConfigError(f"unknown config key [{section}].{key}")
            field, types, label, convert = spec
            rejected_bool = isinstance(value, bool) and bool not in types
            if rejected_bool or not isinstance(value, types):
                raise ConfigError(f"[{section}].{key} must be {label}, got {type(value).__name__}")
            overrides[field] = convert(section, key, value) if convert else value
    return overrides
