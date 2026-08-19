"""Programmatic reads and comment-preserving writes to config.toml.

The config file is both hand-authored and app-written (the web settings panel, the ``add_mcp_server``
tool, and the assistant's own ``update_config`` tool all write it). stdlib ``tomllib`` only reads TOML,
so writes go through ``tomlkit``, which patches the parsed document in place and so keeps the user's
comments and formatting. ``settings.py`` still owns config reads and validation; this module owns the
write path and the policy that guards it.

When the file does not exist yet, the first write seeds it from the shipped example (``config init``'s
content) so a fresh file keeps its documentation rather than becoming a bare one-key stub.

Single-user app: writes are last-writer-wins. A hand-edit made while the app is also writing can be
clobbered; that is accepted rather than guarded with file locking.

Nothing here formats a sentence. ``apply_setting`` returns what it did or raises; the ``config``
toolset in ``toolsets/config.py`` words it for the model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import tomlkit
from tomlkit import TOMLDocument

from kokua.config import file as settings

logger = logging.getLogger(__name__)


def name_from_url(url: str) -> str:
    """A server name derived from its host, for a server added without one.

    Names are the namespace an agent declares against, so every server needs one. A host is stable,
    readable, and already unique per server in practice.
    """
    host = urlparse(url).hostname or "mcp"
    return host.replace(".", "-")


def disambiguate_name(base: str, used: set[str]) -> str:
    """``base``, or ``base-2``, ``base-3``, ... -- the first not already in ``used``.

    Two servers on one host (a service exposing several MCP endpoints under one domain) derive the same
    ``name_from_url`` base; shared by every caller that turns a URL into a registry name (the
    ``add_mcp_server`` config write and the ``--mcp`` CLI flag), so a config that names two collides the
    same way regardless of which of them derived the name.

    These two live with the write that mints the name rather than in ``mcp/servers.py``, where they used
    to: ``config`` is the bottom layer and imports nothing above it, which is what lets
    ``mcp/servers.py`` record a runtime-added server here.
    """
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _load(path: Path) -> TOMLDocument:
    """Parse the file for editing, seeding from the shipped example if it does not exist yet.

    That seed includes the shipped example's ``[agents.*]`` tables: if the file is deleted mid-session,
    the next write here (from ``update_config`` or ``add_mcp_server``) recreates it, default agents and
    all. The content is exactly what ``kokua config init`` would write, so this is not a way to grant a
    capability beyond what a hand-edit already could -- but it is a path by which ``[agents.*]`` reappears
    on disk without a human typing it.
    """
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.parse(settings.example_text())


def _write(path: Path, doc: TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _section_table(doc: TOMLDocument, section: str, *, create: bool):
    """The table at a (possibly dotted) ``section`` path, walked segment by segment.

    ``doc["assistant.generation"] = table`` writes a *quoted* header -- a top-level key whose name
    contains a dot -- which is not the sub-table of ``[assistant]`` the reader looks in. Walking the
    segments is what makes the write land where ``load`` reads. Returns ``None`` for a missing path
    when ``create`` is False.
    """
    table = doc
    for segment in section.split("."):
        nested = table.get(segment)
        if nested is None:
            if not create:
                return None
            nested = tomlkit.table()
            table[segment] = nested
        table = nested
    return table


def set_value(path: Path, section: str, key: str, value) -> None:
    """Set ``[section].key = value``, creating the section if absent, preserving everything else."""
    doc = _load(path)
    _section_table(doc, section, create=True)[key] = value
    _write(path, doc)


def unset_value(path: Path, section: str, key: str) -> None:
    """Remove ``[section].key`` if present; a missing section or key is a no-op."""
    doc = _load(path)
    table = _section_table(doc, section, create=False)
    if table is not None and key in table:
        del table[key]
        _write(path, doc)


def _server_array(doc: TOMLDocument):
    """The ``[[mcp.server]]`` array-of-tables, created (with its ``[mcp]`` parent) if absent."""
    mcp = doc.get("mcp")
    if mcp is None:
        mcp = tomlkit.table()
        doc["mcp"] = mcp
    servers = mcp.get("server")
    if servers is None:
        servers = tomlkit.aot()
        mcp["server"] = servers
    return servers


def _unique_name(servers, url: str) -> str:
    """A name derived from ``url``, disambiguated against every name already in ``servers``.

    Appending a numeric suffix until the name is free (see ``disambiguate_name``) keeps a successful
    ``add_mcp_server`` call from producing a config the registry's collision check would later reject at
    boot, deferred past the point where the tool could still report the problem usefully.
    """
    used = {entry.get("name") for entry in servers}
    return disambiguate_name(name_from_url(url), used)


def add_mcp_server(path: Path, url: str, token_env: str | None = None) -> None:
    """Append (or, by URL, replace) an ``[[mcp.server]]`` entry.

    A runtime-added server (OAuth or unauthenticated) is recorded with just its URL and a name derived
    from it, so it reconnects on the next restart; ``token_env`` is written only for a bearer-token
    server declared explicitly. The derived name reaches no agent until a human names it in
    ``[agents.*]``, since that section is hand-edit only and this write cannot grant capability.

    Replacing an existing entry for the same URL keeps that entry's ``name`` rather than re-deriving
    one, since a human may have hand-edited it to match an ``[agents.*]`` reference that this write
    cannot see or repair. A brand-new entry gets a freshly derived name, disambiguated against every
    name already on file so this call can never write a name collision the registry would reject later.
    """
    doc = _load(path)
    servers = _server_array(doc)
    existing_name = None
    for i, entry in enumerate(servers):
        if entry.get("url") == url:
            existing_name = entry.get("name")
            del servers[i]
            break
    table = tomlkit.table()
    table["url"] = url
    table["name"] = existing_name or _unique_name(servers, url)
    if token_env is not None:
        table["token_env"] = token_env
    servers.append(table)
    _write(path, doc)


def remove_mcp_server(path: Path, url: str) -> bool:
    """Remove the ``[[mcp.server]]`` entry with this URL. Returns whether one was removed."""
    doc = _load(path)
    servers = _server_array(doc)
    for i, entry in enumerate(servers):
        if entry.get("url") == url:
            del servers[i]
            _write(path, doc)
            return True
    return False


# --- The programmatic-write policy and the apply path ------------------------------------------------

# Keys no programmatic write may change, even behind the approval prompt: the tool-approval gate
# itself, the locked email recipient, and where all state lives. Only a hand-edit of config.toml
# changes these.
LOCKED_KEYS: frozenset[tuple[str, str]] = frozenset(
    {("security", "confirm_tools"), ("email", "to"), ("paths", "data_dir")}
)


class SettingLocked(Exception):
    """This key may only be changed by hand-editing config.toml. Carries the section and key."""

    def __init__(self, section: str, key: str):
        super().__init__(f"[{section}].{key}")
        self.section = section
        self.key = key


class HotApplyFailed(Exception):
    """A hot-appliable setting could not be applied to the running session, so nothing was written.

    Its message is the underlying failure's, so a caller can report the cause without unwrapping.
    """

    def __init__(self, section: str, key: str, cause: BaseException):
        super().__init__(str(cause))
        self.section = section
        self.key = key
        self.cause = cause


@dataclass(frozen=True)
class AppliedSetting:
    """What :func:`apply_setting` did. ``hot`` says it also took effect in the running session."""

    section: str
    key: str
    value: object
    hot: bool


def is_locked(section: str, key: str) -> bool:
    """Whether only a hand-edit may change this key.

    ``[agents.*]`` is locked wholesale, and by prefix rather than by an entry in :data:`LOCKED_KEYS`,
    because a section name is per-agent (``agents.<name>``) and so cannot be enumerated ahead of time.
    It declares what every agent can do, and ``update_config`` is a tool the assistant holds, so a
    writable agent table would let the assistant widen its own reach. Granting a capability stays a
    human decision.
    """
    return (section, key) in LOCKED_KEYS or section == "agents" or section.startswith("agents.")


def read_text(path: Path) -> str | None:
    """The config file's contents, or ``None`` when it does not exist yet."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


async def apply_setting(
    path: Path,
    section: str,
    key: str,
    raw: str,
    apply_hot: Callable[[str, str, object], Awaitable[None]],
    *,
    table,
    extra_schema: Optional[dict] = None,
) -> AppliedSetting:
    """Coerce, apply, and persist one setting. Raises rather than reporting.

    A hot-appliable change (the model, the display flags, and whatever the installed toolsets declared
    as hot) is applied to the live session BEFORE it is written, so a value that fails to apply -- an
    invalid model, say -- is not persisted and left to break the next startup. That ordering is the whole
    reason this is one function rather than a coerce call and a write call at the call site.

    ``table`` is the live :class:`~kokua.config.table.SettingsTable`: it decides both what a value coerces
    to and whether the change is hot, so both questions are answered by the same declaration. ``table``
    alone is not the whole schema, though: it holds only hot settings, so ``extra_schema`` carries the
    *cold* keys the installed toolsets declared, without which this refuses one as an unknown key.

    Raises :class:`SettingLocked`, ``ConfigError`` (from coercion), or :class:`HotApplyFailed`.
    """
    if is_locked(section, key):
        raise SettingLocked(section, key)
    coerced = settings.coerce_config_string(section, key, raw, table=table, extra_schema=extra_schema)

    if table.is_hot(section, key):
        try:
            await apply_hot(section, key, coerced)
        except Exception as error:
            logger.warning("Could not apply [%s].%s live", section, key, exc_info=True)
            raise HotApplyFailed(section, key, error) from error
        set_value(path, section, key, coerced)
        return AppliedSetting(section, key, coerced, hot=True)

    set_value(path, section, key, coerced)
    return AppliedSetting(section, key, coerced, hot=False)
