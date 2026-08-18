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
from typing import Any, Callable, Optional, Sequence

from kokua.config import paths as paths
from kokua.config import table as runtime_settings
from kokua.config.schema import AgentConfig, MCPServerConfig

EXAMPLE_FILENAME = "config.example.toml"


class ConfigError(Exception):
    """The config file exists but has a missing-required or wrong-typed value."""


def example_text() -> str:
    """The shipped example config: every key at its built-in default, all documented."""
    # `files("kokua")`, not `files(__package__)`: the example ships at the package root (that is what
    # [tool.setuptools.package-data] declares and what README links to), while this module lives in
    # kokua.config. An editable install would not notice the difference; a built wheel would.
    return importlib.resources.files("kokua").joinpath(EXAMPLE_FILENAME).read_text(encoding="utf-8")


def _str_list(section: str, key: str, value: list) -> list[str]:
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"[{section}].{key} must be a list of strings")
    return list(value)


def _parse_mcp_servers(value: Any) -> list[MCPServerConfig]:
    """Validate the [[mcp.server]] array of tables into a list of ``MCPServerConfig``."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError("[[mcp.server]] must be an array of tables")
    servers: list[MCPServerConfig] = []
    for entry in value:
        url = entry.get("url")
        if not isinstance(url, str):
            raise ConfigError("[[mcp.server]] requires a string 'url'")
        token_env = entry.get("token_env")
        if token_env is not None and not isinstance(token_env, str):
            raise ConfigError(f"[[mcp.server]].token_env must be a string (server {url})")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"[[mcp.server]] requires a string 'name' (server {url}): a server is named so an "
                "agent can declare it in its tools list."
            )
        unknown = set(entry) - {"url", "token_env", "name"}
        if unknown:
            raise ConfigError(f"unknown key(s) in [[mcp.server]] (server {url}): {', '.join(sorted(unknown))}")
        servers.append(MCPServerConfig(url=url, name=name, token_env=token_env))
    return servers


# String-list agent keys share the _str_list validator.
_AGENT_LIST_KEYS = {"tools", "delegates_to"}
_AGENT_KEYS = {
    "description": str,
    "system_message": str,
    "tools": list,
    "delegates_to": list,
}
# Per-agent keys the old [subagents.roles.*] vocabulary used, now folded into the single `tools` list:
# a group, a tool-pack, and an MCP server reference all named a toolset by a different vocabulary word,
# and the registry that replaced that vocabulary makes the distinction moot.
_REMOVED_AGENT_KEYS = {"groups", "tool_packs", "mcp_servers"}


def _parse_agent(name: str, spec: Any) -> AgentConfig:
    """Validate one [agents.<name>] table into an ``AgentConfig``."""
    if not isinstance(spec, dict):
        raise ConfigError(f"[agents.{name}] must be a table")
    fields: dict = {}
    for key, value in spec.items():
        if key in _REMOVED_AGENT_KEYS:
            raise ConfigError(
                f"[agents.{name}].{key} is gone. List it in [agents.{name}].tools instead; every "
                "toolset (AIMU group, plugin, or MCP server) is named the same way there now."
            )
        expected = _AGENT_KEYS.get(key)
        if expected is None:
            raise ConfigError(f"unknown config key [agents.{name}].{key}")
        if key in _AGENT_LIST_KEYS:
            fields[key] = _str_list(f"agents.{name}", key, value)
        elif not isinstance(value, expected):
            raise ConfigError(f"[agents.{name}].{key} must be a {expected.__name__}")
        else:
            fields[key] = value
    return AgentConfig(**fields)


# (section, key) -> (target, accepted TOML types, human label, optional converter), where a target is an
# AssistantConfig field name, or "<toolset>.<key>" for a key a toolset owns (see `_coerce_flat`).
# `bool` is an int subclass, so it is rejected for numeric fields unless explicitly accepted.
#
# Startup-only keys are declared here; the runtime-mutable ones (model, the display flags, and each
# toolset's hot settings) come from the settings table, so the two never drift. A toolset's *cold* keys
# are neither: they come from `settings_sources.startup_schema`. `build_schema` joins all three.
_STARTUP_SCHEMA: dict[tuple[str, str], tuple[str, tuple[type, ...], str, Optional[Callable]]] = {
    ("assistant", "system_message"): ("system_message", (str,), "a string", None),
    ("assistant", "agent"): ("entry_agent", (str,), "a string", None),
    ("assistant", "concurrent_tools"): ("concurrent_tools", (bool,), "a boolean", None),
    ("assistant", "load_plugins"): ("load_plugins", (bool,), "a boolean", None),
    ("assistant", "agent_cache_cap"): ("agent_cache_cap", (int,), "an integer", None),
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
    ("logging", "level"): ("log_level", (str,), "a string", None),
}


# The tables ``load`` parses itself, key by key, rather than through the flat schema above: each has its
# own branch in ``load`` because it maps to one dict/list field or a nested table, not to one field per key.
_STRUCTURED_SECTIONS = frozenset({"subagents", "agents", "mcp"})


def core_sections() -> frozenset[str]:
    """Every ``config.toml`` section Kokua's own core parses.

    A toolset's settings section is always the toolset's own name, so a toolset named after one of these
    would claim a section the core already owns. Because a contributed entry wins the merge in
    :func:`build_schema`, that key would then parse into the toolset's bucket while the ``AssistantConfig``
    field behind it stayed at its default -- a capability silently switching off in a config the user never
    edited. ``config.settings_sources`` rejects such a name at startup.

    Derived from the schema and the structured tables rather than hand-listed at the place that checks it,
    so the reserved set cannot drift from the sections it exists to protect. ``_REMOVED_KEYS`` is unioned
    in too: a toolset named e.g. ``tools`` would otherwise pass this check and then hit ``load``'s
    removed-key branch first regardless, since that branch is checked before the schema -- permanently
    unparseable behind "[tools] is gone." rather than refused with the settings-collision message.
    """
    return frozenset(
        {section for section, _ in _STARTUP_SCHEMA}
        | {setting.section for setting in runtime_settings.CORE_RUNTIME_SETTINGS}
        | _STRUCTURED_SECTIONS
        | {section for section, _ in _REMOVED_KEYS}
    )


def build_schema(table, extra: Optional[dict] = None) -> dict:
    """The full TOML schema: the startup-only keys, the settings table's, and any extra entries.

    Takes the table rather than importing one, because the contributed half comes from the installed
    toolsets and ``config`` is the bottom layer: it may not import ``kokua.toolsets`` or
    ``kokua.plugins``. The caller that knows both (``kokua.cli``) passes both in. ``extra`` carries the
    *cold* keys a toolset declared, which the table does not hold because it holds only hot ones.

    A contributed entry wins over a startup-only one of the same name, which is what lets a capability take
    over a key the core used to own. That only reads as an upgrade because a toolset cannot be *named* after
    a section the core still parses (see :func:`core_sections`).
    """
    return {**_STARTUP_SCHEMA, **table.toml_schema(), **(extra or {})}


def _coerce_flat(schema: dict, section: str, key: str, value: Any) -> tuple[str, Any]:
    """Validate one scalar ``[section].key`` against ``schema``; return ``(target, coerced)``.

    ``target`` is an ``AssistantConfig`` field name for a core key, or ``"<toolset>.<key>"`` for one a
    toolset owns; ``load`` routes the two differently.
    """
    spec = schema.get((section, key))
    if spec is None:
        raise ConfigError(f"unknown config key [{section}].{key}")
    target, types, label, convert = spec
    rejected_bool = isinstance(value, bool) and bool not in types
    if rejected_bool or not isinstance(value, types):
        raise ConfigError(f"[{section}].{key} must be {label}, got {type(value).__name__}")
    return target, (convert(section, key, value) if convert else value)


def _parse_scalar(section: str, key: str, raw: str, types: tuple[type, ...]) -> Any:
    """Parse a string from the ``update_config`` tool into the TOML type the schema expects."""
    if bool in types:
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        raise ConfigError(f"[{section}].{key} must be true or false, got {raw!r}")
    if list in types:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if int in types:
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"[{section}].{key} must be an integer, got {raw!r}")
    return raw


def coerce_config_string(section: str, key: str, raw: str, *, table, extra_schema: Optional[dict] = None) -> Any:
    """Validate and coerce a string value from the ``update_config`` tool into its config type.

    The tool passes every value as a string (LLM-friendly, avoids a union-typed argument); this maps
    it onto the type the config schema expects. Raises ``ConfigError`` with a user-facing message for an
    unknown key, a wrong-typed/out-of-range value, or a structured section that has no scalar entries.

    ``extra_schema`` is the same *cold* toolset keys ``load`` takes, and for the same reason: the table
    holds only hot settings, so without it a key a toolset declared as cold is not in any schema this can
    see and the tool answers "unknown config key" for a key sitting in the user's file. It is passed in,
    never imported, because ``config`` is the bottom layer and cannot reach the installed toolsets.
    """
    if section in ("subagents", "agents", "mcp"):
        raise ConfigError(f"[{section}] has no scalar keys editable with update_config")
    spec = build_schema(table, extra_schema).get((section, key))
    if spec is None:
        raise ConfigError(f"unknown config key [{section}].{key}")
    _, types, _, convert = spec
    value = _parse_scalar(section, key, raw, types)
    return convert(section, key, value) if convert else value


# Keys this release removed, mapped to the message naming their replacement. Checked before the schema
# so an old config gets a targeted error instead of a generic unknown-key one. A `None` key matches the
# whole section, for a table that no longer exists at all regardless of which key inside it is set.
_REMOVED_KEYS = {
    ("tools", None): "[tools] is gone. Each agent now lists what it holds in [agents.<name>].tools.",
    ("subagents", "concurrent"): "[subagents].concurrent is now [assistant].concurrent_tools.",
    ("assistant", "memory"): (
        "[assistant].memory is gone. Declare the 'memory' and 'documents' toolsets in an agent's "
        "tools list instead; declaring a toolset is the only switch."
    ),
}


def resolve_path(explicit: Optional[str]) -> tuple[Path, bool]:
    """Return the config-file path and whether the user explicitly requested it."""
    if explicit:
        return Path(explicit).expanduser(), True
    env = os.environ.get("KOKUA_CONFIG")
    if env:
        return Path(env).expanduser(), True
    return paths.config_path(), False


def load(
    explicit: Optional[str] = None,
    *,
    table,
    extra_schema: Optional[dict] = None,
    declaring_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Parse the config file into a dict of ``AssistantConfig`` field overrides.

    ``table`` is the live :class:`kokua.config.table.SettingsTable`, which carries the hot sections the
    installed toolsets own, and ``extra_schema`` their cold keys. ``table`` is a required keyword rather
    than a default, because a default would silently parse a toolset's section as an unknown key.

    A key a toolset owns is returned nested under ``"toolset_settings"`` rather than as a flat field,
    since it has no ``AssistantConfig`` field to be one.

    ``declaring_names`` is every toolset name that may own a section (passed in because ``config`` may
    not import ``kokua.toolsets``, the module that knows them); when given, the overrides carry a
    ``"configured_sections"`` entry: the intersection of those names with the file's own top-level
    section headers. This is a name check on the file's *headers*, not on the parsed key/value overrides
    above, because a section can be present with every key commented out (the shipped
    ``config.example.toml``'s ``[planning]`` is exactly that): such a section sets nothing, so it would
    otherwise leave no trace for the startup warning that a configured-but-undeclared section exists to
    catch.

    The file is required, not optional. Agents live only in it and the assistant cannot run without
    at least one, so "no config" is not a state Kokua can start in; failing here with the command that
    fixes it beats starting something that cannot work.
    """
    path, _ = resolve_path(explicit)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}\nRun `kokua config init` to write one.")

    with path.open("rb") as file:
        data = tomllib.load(file)

    schema = build_schema(table, extra_schema)
    overrides: dict[str, Any] = {}
    for section, entries in data.items():
        if not isinstance(entries, dict):
            raise ConfigError(f"top-level config key {section!r} is not a [section] table")
        if (section, None) in _REMOVED_KEYS:
            raise ConfigError(_REMOVED_KEYS[(section, None)])
        # [subagents] no longer holds anything live: `concurrent` moved to [assistant].concurrent_tools,
        # and `roles` moved to [agents.<name>], so both keys are checked against the removed-key table.
        if section == "subagents":
            for key, value in entries.items():
                if (section, key) in _REMOVED_KEYS:
                    raise ConfigError(_REMOVED_KEYS[(section, key)])
                if key == "roles":
                    raise ConfigError("[subagents.roles.<name>] is gone. Declare each agent whole in [agents.<name>].")
                raise ConfigError(f"unknown config key [subagents].{key}")
            continue
        # The [agents] table holds one sub-table per agent, each parsed into an AgentConfig, so it is
        # handled specially like [subagents]/[mcp] rather than via the schema's flat one-key-one-target map.
        if section == "agents":
            overrides["agents"] = {name: _parse_agent(name, spec) for name, spec in entries.items()}
            continue
        # The [mcp] table holds a [[mcp.server]] array of tables (each url + optional token_env),
        # not flat scalar keys, so it is handled specially like [subagents]/[agents].
        if section == "mcp":
            for key, value in entries.items():
                if key != "server":
                    raise ConfigError(f"unknown config key [mcp].{key}")
                overrides["mcp_servers"] = _parse_mcp_servers(value)
            continue
        for key, value in entries.items():
            if (section, key) in _REMOVED_KEYS:
                raise ConfigError(_REMOVED_KEYS[(section, key)])
            target, coerced = _coerce_flat(schema, section, key, value)
            if "." in target:
                toolset, setting_key = target.split(".", 1)
                overrides.setdefault("toolset_settings", {}).setdefault(toolset, {})[setting_key] = coerced
            else:
                overrides[target] = coerced
    if declaring_names:
        overrides["configured_sections"] = tuple(sorted(set(data) & set(declaring_names)))
    return overrides
