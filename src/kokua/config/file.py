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
from typing import Any, Callable, Optional, Sequence, Union

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


# Where the write policy is declared, and the name of the `AssistantConfig` field it loads into, which is
# the same word. Named rather than spelled three times, because `load` has to find the parsed patterns
# again by their field name to run the semantic check on them, and to say where they came from.
_LOCK_LIST_SECTION = "security"
_LOCK_LIST_KEY = "locked_config_keys"

# The three accepted pattern forms, spelled out. Every rejection below ends with this, because a pattern
# the user has to retype is only useful next to what a good one looks like.
_LOCK_PATTERN_FORMS = (
    'Write "section.key" for one key, "section.*" for a section and everything under it, or "*" for every key.'
)


def _lock_pattern_fault(pattern: str) -> Optional[str]:
    """Why ``pattern`` could never match a key, or None when it is one of the three accepted forms.

    ``store.locked_by`` compares a pattern to a section and a key literally, segment for segment, so
    every fault named here describes a pattern that would be *accepted in silence and lock nothing*:
    a security setting whose file still reads as though the policy were in force. That is the failure
    this validator exists to prevent, which is why the check is the whole shape of a pattern rather
    than only the dotless case a reader hits first.

    A faulty pattern is rejected rather than repaired, whitespace included. The list is hand-authored,
    so quietly stripping a stray space would hide the slip that produced it instead of showing the user
    a lock they believe they wrote and do not have.

    Only the *shape* is decided here. Whether the section and key a well-shaped pattern names could ever
    exist is the second check, :func:`_lock_target_fault`, which needs the schema this one cannot see.
    ``"Agents.*"`` is the pattern that shows the split: its shape is perfect, TOML keys are
    case-sensitive, so it matches nothing, and only a list of the sections that exist can say so.
    """
    if not pattern:
        return "it is empty"
    if pattern != pattern.strip():
        return "it has leading or trailing whitespace, and a pattern is matched exactly as written"
    if pattern == "*":
        return None
    segments = pattern.split(".")
    if len(segments) == 1:
        return "it has no dot, so it names no key"
    for position, segment in enumerate(segments):
        if not segment:
            return "it has an empty segment"
        if segment != segment.strip():
            return f"the segment {segment!r} has whitespace around it"
        if "*" in segment and segment != "*":
            return f"the segment {segment!r} mixes '*' with other characters, and '*' stands for a whole segment"
        if segment == "*" and position != len(segments) - 1:
            return "'*' stands for the rest of the pattern, so it means nothing except in the last segment"
    return None


def _locked_config_keys(section: str, key: str, value: list) -> list[str]:
    """Validate the write-policy patterns, rejecting one that could never match.

    Failing at startup is what keeps a typo in a security setting from reading as a policy that is in
    force: an unmatchable pattern locks nothing, and both the file and the policy preamble
    ``read_config`` prints still show the line the user wrote. See :func:`_lock_pattern_fault` for the
    forms that are wrong and why each one is silent, and :func:`_lock_target_fault` for the vocabulary
    check ``load`` runs afterwards, which is where a well-shaped pattern naming nothing real is caught.
    """
    patterns = _str_list(section, key, value)
    for pattern in patterns:
        fault = _lock_pattern_fault(pattern)
        if fault is not None:
            raise ConfigError(f"[{section}].{key}: {pattern!r} matches nothing, because {fault}. {_LOCK_PATTERN_FORMS}")
    return patterns


# The reasoning-effort levels AIMU accepts as strings; `true` / `false` are the other two forms.
_THINKING_LEVELS = ("low", "medium", "high")


def _thinking(section: str, key: str, value: Any) -> Any:
    """Validate one reasoning-effort value into AIMU's own vocabulary: a bool, or a level string.

    Validated here rather than left to AIMU because AIMU raises on an unrecognized level only once a
    request is built, so a typo would surface mid-turn instead of at startup naming the table it came
    from -- and ``"xhigh"`` is a plausible one, being Qwen's own effort ceiling.

    A ``"true"`` / ``"false"`` *string* is accepted as the bool it spells, because ``update_config``
    passes every value as a string and this is the only validator that sees it; the same reason
    ``_parse_scalar`` reads those two words for a bool-typed key.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        if lowered in _THINKING_LEVELS:
            return lowered
    raise ConfigError(f"[{section}].{key} must be true, false, or one of {', '.join(_THINKING_LEVELS)}, got {value!r}")


# The wire word for "do not reason". A message frame carries strings, and `False` is not one; the levels
# are `_THINKING_LEVELS` above. There is deliberately no word for "use the configured effort", because
# asking for that is the absence of a request.
_THINKING_OFF = "off"


def thinking_request(value: Any) -> Optional[Union[bool, str]]:
    """One per-turn reasoning-effort request off a channel message, or None to leave the config in force.

    The channel counterpart of ``_thinking`` above, and deliberately the softer of the two. A value in
    ``config.toml`` is a declaration the user wrote, so a typo there stops startup and names the table it
    came from; a value on a message is transport input that arrived while someone is waiting for an
    answer, so anything unrecognized degrades to the configured effort rather than failing the turn.

    Both read ``_THINKING_LEVELS``, which is why this lives here rather than beside either channel: the
    two entry points cannot drift apart about what a level is.
    """
    if not isinstance(value, str):
        return None
    choice = value.strip().lower()
    if choice == _THINKING_OFF:
        return False
    return choice if choice in _THINKING_LEVELS else None


# Every generation parameter Kokua sets, with the range each accepts: (accepted TOML types, the label
# an error shows, the range predicate). One table because both tiers read it -- the flat schema entries
# for [assistant.generation] and the whole-table validator for [agents.<name>.generation] -- so the two
# cannot disagree about what a key means, the same arrangement `_thinking` has.
#
# `int` is accepted for the float keys because TOML `temperature = 1` is an int, and every backend takes
# one. Range-checking here rather than leaving it to the provider is what turns a typo into a startup
# error naming the table instead of a rejected request mid-turn.
_GENERATION_KEYS: dict[str, tuple[tuple[type, ...], str, Callable[[Any], bool]]] = {
    "temperature": ((int, float), "a number from 0.0 to 2.0", lambda v: 0.0 <= v <= 2.0),
    "top_p": ((int, float), "a number from 0.0 to 1.0", lambda v: 0.0 <= v <= 1.0),
    "top_k": ((int,), "an integer of at least 1", lambda v: v >= 1),
    "min_p": ((int, float), "a number from 0.0 to 1.0", lambda v: 0.0 <= v <= 1.0),
    "presence_penalty": ((int, float), "a number from -2.0 to 2.0", lambda v: -2.0 <= v <= 2.0),
    "repetition_penalty": ((int, float), "a number greater than 0.0", lambda v: v > 0.0),
    "max_tokens": ((int,), "an integer of at least 1", lambda v: v >= 1),
    "context_length": ((int,), "an integer of at least 1", lambda v: v >= 1),
}

# The sub-table of [assistant] the default tier lives in. A dotted section, so one schema entry per key
# serves it and `update_config` can write one; nothing can collide with the name, since a toolset name
# has no dot in it.
_GENERATION_SECTION = "assistant.generation"


def _generation_value(section: str, key: str, value: Any) -> Any:
    """Validate one generation parameter, naming the table it came from.

    Shared by both tiers: the flat schema calls it as a converter for ``[assistant.generation]``, and
    ``_generation`` calls it per entry for an agent's own table.
    """
    spec = _GENERATION_KEYS.get(key)
    if spec is None:
        raise ConfigError(f"unknown config key [{section}].{key}. Accepted: {', '.join(sorted(_GENERATION_KEYS))}")
    types, label, in_range = spec
    # `bool` is an int subclass, so `temperature = true` would otherwise pass as a number.
    if isinstance(value, bool) or not isinstance(value, types):
        raise ConfigError(f"[{section}].{key} must be {label}, got {type(value).__name__}")
    if not in_range(value):
        raise ConfigError(f"[{section}].{key} must be {label}, got {value!r}")
    return value


def _generation(section: str, key: str, value: Any) -> dict:
    """Validate a ``[<agent>.generation]`` sub-table into a dict of checked parameters."""
    if not isinstance(value, dict):
        raise ConfigError(f"[{section}.{key}] must be a table")
    table = f"{section}.{key}"
    return {name: _generation_value(table, name, item) for name, item in value.items()}


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
# Agent keys whose value set is closed rather than merely typed, so a validator decides instead of an
# isinstance check. `thinking` is a bool-or-string union besides, which the type map below cannot express.
_AGENT_VALIDATED_KEYS = {"thinking": _thinking, "generation": _generation}
_AGENT_KEYS = {
    "description": str,
    "system_message": str,
    "model": str,
    "thinking": object,  # value set checked by _AGENT_VALIDATED_KEYS; listed here so it is a known key
    "generation": object,  # a sub-table, validated by _AGENT_VALIDATED_KEYS; listed here for the same reason
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
        elif key in _AGENT_VALIDATED_KEYS:
            fields[key] = _AGENT_VALIDATED_KEYS[key](f"agents.{name}", key, value)
        elif not isinstance(value, expected):
            raise ConfigError(f"[agents.{name}].{key} must be a {expected.__name__}")
        else:
            fields[key] = value
    return AgentConfig(**fields)


# The dotted section `_sections` yields for the [scheduling.task.<name>] tables. Not a member of
# _STRUCTURED_SECTIONS: that set feeds `core_sections`, and reserving "scheduling" there would make
# `settings_sources` reject the scheduling toolset's own max_task_conversations as a section collision.
# A dotted name is safe to route on because a toolset name may not contain a '.'.
_TASK_SECTION = "scheduling.task"

# Per schedule type, the keys it requires: accepted TOML types plus the label an error uses. Only the
# shape is checked here. Whether "25:99" is a real time is recurrence math, and `scheduling` sits above
# `config`, which imports nothing above it; a structurally valid but unsatisfiable schedule still shows
# up as STATUS_INVALID when the task is armed, exactly as it does today.
_SCHEDULE_KEYS: dict[str, dict[str, tuple]] = {
    "once": {"at": (str, "string")},
    "interval": {"seconds": ((int, float), "number")},
    "daily": {"at": (str, "string")},
    "weekly": {"at": (str, "string"), "day": (str, "string")},
}

_TASK_KEYS = {
    "prompt": str,
    "schedule": dict,
    "enabled": bool,
    "max_conversations": int,
    "created_at": str,
    "fired_at": str,
}


def _parse_schedule(name: str, value: dict) -> dict:
    """Validate one task's schedule table, naming the task in every message."""
    kind = value.get("type")
    required = _SCHEDULE_KEYS.get(kind)
    if required is None:
        raise ConfigError(
            f"[scheduling.task.{name}].schedule.type must be one of {', '.join(sorted(_SCHEDULE_KEYS))}; got {kind!r}"
        )
    unknown = set(value) - set(required) - {"type"}
    if unknown:
        raise ConfigError(f"unknown key(s) in [scheduling.task.{name}].schedule: {', '.join(sorted(unknown))}")
    for key, (types, label) in required.items():
        if key not in value:
            raise ConfigError(f"[scheduling.task.{name}].schedule requires {key!r} for a {kind!r} schedule")
        entry = value[key]
        if isinstance(entry, bool) or not isinstance(entry, types):
            raise ConfigError(f"[scheduling.task.{name}].schedule.{key} must be a {label}")
    return dict(value)


def parse_task(name: str, spec: Any) -> dict:
    """Validate one [scheduling.task.<name>] table into a task record, its name folded in.

    Public, unlike ``_parse_agent``, because ``config.store`` reuses it for the live re-read
    ``TaskService`` does on every operation: one validator means the startup path and the live path
    cannot drift in what they accept.

    Returns a plain dict rather than a dataclass. A task record travels through the service, the tool
    surface, and the web frame as a dict already, so a dataclass here would only be unpacked at both
    ends.
    """
    if not isinstance(spec, dict):
        raise ConfigError(f"[scheduling.task.{name}] must be a table")
    record: dict = {"name": name}
    for key, value in spec.items():
        expected = _TASK_KEYS.get(key)
        if expected is None:
            raise ConfigError(f"unknown config key [scheduling.task.{name}].{key}")
        if key == "schedule":
            if not isinstance(value, dict):
                raise ConfigError(f"[scheduling.task.{name}].schedule must be a table")
            record[key] = _parse_schedule(name, value)
            continue
        # bool is an int subclass, so an explicit check keeps `max_conversations = true` from passing.
        if expected is int and isinstance(value, bool):
            raise ConfigError(f"[scheduling.task.{name}].{key} must be an int")
        if not isinstance(value, expected):
            raise ConfigError(f"[scheduling.task.{name}].{key} must be a {expected.__name__}")
        record[key] = value
    for required_key in ("prompt", "schedule"):
        if required_key not in record:
            raise ConfigError(f"[scheduling.task.{name}] requires {required_key!r}")
    if record.get("max_conversations", 0) < 0:
        raise ConfigError(f"[scheduling.task.{name}].max_conversations must be 0 (unlimited) or more")
    return record


# (section, key) -> (target, accepted TOML types, human label, optional converter), where a target is an
# AssistantConfig field name, or "<toolset>.<key>" for a key a toolset owns (see `_coerce_flat`).
# `bool` is an int subclass, so it is rejected for numeric fields unless explicitly accepted.
#
# Startup-only keys are declared here; the runtime-mutable ones (the display flags, and each toolset's
# hot settings) come from the settings table, so the two never drift. A toolset's *cold* keys
# are neither: they come from `settings_sources.startup_schema`. `build_schema` joins all three.
_STARTUP_SCHEMA: dict[tuple[str, str], tuple[str, tuple[type, ...], str, Optional[Callable]]] = {
    # The default model every agent runs on unless its own [agents.<name>].model overrides it. Read once,
    # at startup: an agent's client is built with it and nothing rebinds a live client to another model.
    ("assistant", "model"): ("model", (str,), "a string", None),
    # The default reasoning effort every agent runs at unless its own [agents.<name>].thinking overrides
    # it. Startup-only, like the model above and for the same reason: no runtime writer can reach
    # [agents.*], so a live field there could only ever disagree with a declaration.
    ("assistant", "thinking"): (
        "thinking",
        (bool, str),
        f"true, false, or one of {', '.join(_THINKING_LEVELS)}",
        _thinking,
    ),
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
    (_LOCK_LIST_SECTION, _LOCK_LIST_KEY): (
        _LOCK_LIST_KEY,
        (list,),
        "a list of strings",
        _locked_config_keys,
    ),
    ("paths", "data_dir"): ("data_dir", (str,), "a string path", lambda s, k, v: Path(v).expanduser()),
    ("frontend", "name"): ("frontend", (str,), "a string", None),
    ("mcp", "oauth_callback_host"): ("mcp_oauth_callback_host", (str,), "a string", None),
    ("mcp", "oauth_callback_port"): ("mcp_oauth_callback_port", (int,), "an integer", None),
    ("web", "host"): ("host", (str,), "a string", None),
    ("web", "port"): ("port", (int,), "an integer", None),
    ("logging", "level"): ("log_level", (str,), "a string", None),
}

# One entry per generation parameter, derived from `_GENERATION_KEYS` rather than written out, so a key
# added there reaches the schema (and `update_config`) without a second list to keep in step. The target
# is the `generation` field, but `load` routes this section by name rather than by target: `generation`
# is also a legal toolset name, and the dotted-target convention would send these into that bucket.
_STARTUP_SCHEMA.update(
    {
        (_GENERATION_SECTION, key): ("generation", types, label, _generation_value)
        for key, (types, label, _) in _GENERATION_KEYS.items()
    }
)


def _schema_section(section: str) -> str:
    """The schema's name for a section whose second segment is a user-chosen agent name.

    An agent's table is ``[agents.<name>]``, so its keys cannot be enumerated in a flat schema the way
    every other section's can. Folding the name to ``*`` before the lookup is what lets one set of
    entries serve every agent, and it is the same shape ``store.locked_by`` matches a pattern with.
    A bare ``agents`` is left alone so ``coerce_config_string`` can give it its own error.
    """
    if not section.startswith("agents."):
        return section
    return ".".join(["agents", "*", *section.split(".")[2:]])


# What a folded section is called in error text. `agents.*` is a schema key, not a section any file has.
_FOLDED_PREFIX = "agents.*"
_NAMED_PREFIX = "agents.<name>"


def _named_section(section: str) -> str:
    """A schema section written the way a config file writes it, for error text only.

    The inverse of :func:`_schema_section`, and it exists because a hint is something a model acts on
    literally: told "did you mean [agents.*].tools?", it calls ``update_config`` with that section
    verbatim, and a write policy that has unlocked agent tables then creates an agent named ``*``. The
    placeholder cannot be actioned that way, so a misread costs a second attempt rather than a junk
    table on disk.
    """
    if not section.startswith(_FOLDED_PREFIX):
        return section
    return _NAMED_PREFIX + section[len(_FOLDED_PREFIX) :]


#: What ``update_config`` may write inside an agent's table, keyed by the wildcard section
#: ``_schema_section`` produces. Deliberately not merged into ``_STARTUP_SCHEMA``: no real file has a
#: section literally named ``agents.*``, and keeping these out of the load path means ``load`` cannot
#: route a table through them instead of through ``_parse_agent``. ``generation`` has no entry because it
#: is a sub-table rather than a scalar; ``coerce_config_string`` points that write at
#: ``[agents.<name>.generation]`` instead.
AGENT_SCHEMA: dict[tuple[str, str], tuple] = {
    ("agents.*", "description"): ("description", (str,), "a string", None),
    ("agents.*", "system_message"): ("system_message", (str,), "a string", None),
    ("agents.*", "model"): ("model", (str,), "a string", None),
    ("agents.*", "thinking"): ("thinking", (bool, str), "true, false, or low/medium/high", _thinking),
    ("agents.*", "tools"): ("tools", (list,), "a list of strings", _str_list),
    ("agents.*", "delegates_to"): ("delegates_to", (list,), "a list of strings", _str_list),
    **{
        ("agents.*.generation", key): ("generation", types, label, _generation_value)
        for key, (types, label, _) in _GENERATION_KEYS.items()
    },
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


def _lock_pattern_sections(schema: dict) -> frozenset[str]:
    """Every top-level section name a lock pattern's first segment may name.

    Derived from what the code already knows rather than listed, for the reason :func:`core_sections`
    gives: a hand-written set would answer for a config file it had stopped describing. Three sources,
    because a section reaches ``load`` three ways. The schema holds the flat sections, Kokua's own and
    each installed toolset's, and a dotted one contributes its head, since ``[assistant.generation]`` is
    reached by locking ``assistant.*``. :data:`_STRUCTURED_SECTIONS` holds the tables ``load`` parses
    itself, which have no schema entries to be found in. :data:`_TASK_SECTION` holds ``scheduling``,
    which must be a known section whether or not the scheduling toolset is installed: the shipped
    ``scheduling.task.*`` default has to survive every table this is called with, including the
    core-only one a config test parses through.
    """
    return frozenset(
        {section.split(".")[0] for section, _ in schema} | _STRUCTURED_SECTIONS | {_TASK_SECTION.split(".")[0]}
    )


def _lock_target_fault(schema: dict, pattern: str) -> Optional[str]:
    """Why ``pattern`` names a section or key no config could hold, or None when it could match.

    The second half of the lock-list check, and it cannot be folded into :func:`_lock_pattern_fault`:
    that one runs as a schema converter, whose ``(section, key, value)`` signature is fixed by every
    other converter and carries no view of the schema, and the schema does not exist yet when the
    converter runs, being built in ``load`` from the settings table the caller passes in. So the shape
    is checked on the way in and the vocabulary once the file is parsed.

    Deliberately not a check that the pattern matches something *present*. A ``.*`` pattern is how the
    per-agent and per-task sections get covered, and those names are the user's; ``agents.new_helper.*``
    has to keep loading for an agent added tomorrow. What is caught is a first segment that is not a
    section at all, and, for an exact ``<section>.<key>`` in a flat section, a key that section does not
    have. A misspelled *agent* or *task* name inside a pattern is not caught by either, since there is no
    closed set to check it against.
    """
    if pattern == "*":
        return None
    known = _lock_pattern_sections(schema)
    first = pattern.split(".")[0]
    if first not in known:
        return f"no config section is named {first!r}. Sections: {', '.join(sorted(known))}"
    if pattern.endswith(".*"):
        return None
    section, _, key = pattern.rpartition(".")
    # Only a section the schema enumerates can answer this. A dotted section holding user-named
    # sub-tables ([agents.<name>], [scheduling.task.<name>]) has no entries here and is left alone.
    keys = {entry_key for entry_section, entry_key in schema if entry_section == section}
    if keys and key not in keys:
        return f"[{section}] has no key {key!r}. Keys in [{section}]: {', '.join(sorted(keys))}"
    return None


def _unknown_key_error(schema: dict, section: str, key: str) -> ConfigError:
    """A "no such key" error that also says where the key *does* live and what this section takes.

    Two hints for the two ways a key gets misplaced: the right key in the wrong section, and a key the
    section has never had. Both are read off the same schema the lookup just missed in, so neither can
    name something that is not really there. It matters most for ``update_config``, whose caller is a
    model retrying from the error text alone -- ``[assistant.generation].thinking`` is the case that
    prompted it, `thinking` being an ``[assistant]`` key whose sub-table sits directly beneath it.

    The section serves two purposes here and an agent's table is why they cannot be the same string. The
    header names the section the caller actually passed, ``[agents.researcher]``, because that is what the
    writer was editing; the hints are looked up under the folded ``agents.*`` the schema is keyed by, or
    every agent-table typo would arrive with no "Accepted in" list, losing the retry aid exactly where the
    accepted keys are least guessable. :func:`_named_section` writes a folded section back out for the
    reader.
    """
    lookup = _schema_section(section)
    message = f"unknown config key [{section}].{key}"
    elsewhere = sorted(other for other, other_key in schema if other_key == key and other != lookup)
    if elsewhere:
        message += "; did you mean " + " or ".join(f"[{_named_section(other)}].{key}" for other in elsewhere) + "?"
    accepted = sorted(k for s, k in schema if s == lookup)
    if accepted:
        message += f" Accepted in [{section}]: {', '.join(accepted)}."
    return ConfigError(message)


def _coerce_flat(schema: dict, section: str, key: str, value: Any) -> tuple[str, Any]:
    """Validate one scalar ``[section].key`` against ``schema``; return ``(target, coerced)``.

    ``target`` is an ``AssistantConfig`` field name for a core key, or ``"<toolset>.<key>"`` for one a
    toolset owns; ``load`` routes the two differently.
    """
    spec = schema.get((section, key))
    if spec is None:
        raise _unknown_key_error(schema, section, key)
    target, types, label, convert = spec
    rejected_bool = isinstance(value, bool) and bool not in types
    if rejected_bool or not isinstance(value, types):
        raise ConfigError(f"[{section}].{key} must be {label}, got {type(value).__name__}")
    return target, (convert(section, key, value) if convert else value)


def _parse_scalar(section: str, key: str, raw: str, types: tuple[type, ...]) -> Any:
    """Parse a string from the ``update_config`` tool into the TOML type the schema expects."""
    # `bool in types` alone would claim a bool-or-string union ([assistant].thinking), refusing a level
    # string for a key whose own converter accepts one. A union's words are that converter's business.
    if bool in types and str not in types:
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        raise ConfigError(f"[{section}].{key} must be true or false, got {raw!r}")
    if list in types:
        return [item.strip() for item in raw.split(",") if item.strip()]
    # Before the int branch, because the float-typed keys declare (int, float) so that TOML's
    # `temperature = 1` parses: `int in types` alone would refuse "0.7" for a key that accepts it.
    if float in types:
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"[{section}].{key} must be a number, got {raw!r}")
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

    An ``[agents.<name>]`` section is resolved through :data:`AGENT_SCHEMA` after ``_schema_section``
    folds the agent's name to ``*``. Whether such a write is *allowed* is not decided here: that is
    ``store.locked_by`` against the user's ``[security].locked_config_keys``, which locks ``agents.*`` by
    default.
    """
    # A bare [agents] write names no agent: the section is [agents.<name>], and writing a key directly
    # under [agents] would produce a table `_parse_agent` reads as an agent named after the key.
    if section == "agents":
        raise ConfigError(
            f'[agents].{key} is not a setting. Name an agent: section="agents.{key}" with the key you '
            'want to set, for instance "tools".'
        )
    if section.startswith("agents.") and key == "generation":
        raise ConfigError(
            f"[{section}].generation is a table, not a scalar. Set one parameter at a time with "
            f'section="{section}.generation", for instance key="temperature".'
        )
    if section == "subagents":
        raise ConfigError("[subagents] has no scalar keys editable with update_config")
    # [mcp] does have scalar keys, but its [[mcp.server]] array is not one of them: a server is added
    # and removed through the mcp-admin tools, which connect it as well as write it.
    if section == "mcp" and key == "server":
        raise ConfigError("[[mcp.server]] is not editable with update_config; use the MCP tools")
    schema = build_schema(table, {**AGENT_SCHEMA, **(extra_schema or {})})
    spec = schema.get((_schema_section(section), key))
    if spec is None:
        raise _unknown_key_error(schema, section, key)
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


def _sections(data: dict) -> list[tuple[str, dict]]:
    """Every ``[section]``, plus each nested ``[section.sub]`` as its own dotted section.

    ``tomllib`` nests ``[assistant.generation]`` inside ``assistant``, where the flat key loop would
    read it as a key whose value is a table. Re-entering it as its own section is what lets one schema
    entry per key serve a sub-table. General rather than special-cased for one key, so any other nested
    table gets an accurate "unknown config key [section.sub].key" instead of a type complaint about a
    dict. The structured sections are handed over whole: each parses its own nested tables.
    """
    sections: list[tuple[str, dict]] = []
    for section, entries in data.items():
        if not isinstance(entries, dict):
            raise ConfigError(f"top-level config key {section!r} is not a [section] table")
        if section in _STRUCTURED_SECTIONS:
            sections.append((section, entries))
            continue
        flat = {}
        for key, value in entries.items():
            if isinstance(value, dict):
                sections.append((f"{section}.{key}", value))
            else:
                flat[key] = value
        sections.append((section, flat))
    return sections


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

    # A syntax error is reported as a ConfigError like every other fault in this file, the conversion
    # `store.load_tasks` also makes: `load` is reached from `update_config`'s dry run as well as from
    # startup, and a bare TOMLDecodeError there escapes the tool call instead of becoming a refusal.
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error

    schema = build_schema(table, extra_schema)
    overrides: dict[str, Any] = {}
    for section, entries in _sections(data):
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
        # [mcp] is the one section holding both shapes: a [[mcp.server]] array of tables, which needs
        # its own parser, and ordinary scalar keys (the OAuth callback), which the schema handles. Only
        # the array is special-cased; everything else falls through to the flat path below.
        if section == "mcp":
            if "server" in entries:
                overrides["mcp_servers"] = _parse_mcp_servers(entries["server"])
            entries = {key: value for key, value in entries.items() if key != "server"}
        # Routed by section rather than by the dotted-target convention a toolset's keys use, because
        # `generation` is also a legal toolset name and would claim the same bucket.
        if section == _GENERATION_SECTION:
            for key, value in entries.items():
                _, coerced = _coerce_flat(schema, section, key, value)
                overrides.setdefault("generation", {})[key] = coerced
            continue
        # A dotted section like _GENERATION_SECTION above, and routed the same way: `_sections` hands
        # the [scheduling.task.*] tables over as one {name: table} mapping, and each is a whole task
        # rather than one key of a flat section.
        if section == _TASK_SECTION:
            overrides["scheduled_tasks"] = {name: parse_task(name, spec) for name, spec in entries.items()}
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
    # Run once the whole file is parsed, because it is the only point where both halves exist: the
    # patterns the user wrote, and the schema naming every section this install really has.
    for pattern in overrides.get(_LOCK_LIST_KEY, ()):
        fault = _lock_target_fault(schema, pattern)
        if fault is not None:
            raise ConfigError(f"[{_LOCK_LIST_SECTION}].{_LOCK_LIST_KEY}: {pattern!r} matches nothing, because {fault}")
    if declaring_names:
        overrides["configured_sections"] = tuple(sorted(set(data) & set(declaring_names)))
    return overrides
