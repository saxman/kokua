"""Comment-preserving writes to config.toml.

The config file is both hand-authored and app-written (the web settings panel, the ``add_mcp_server``
tool, and the assistant's own ``update_config`` tool all write it). stdlib ``tomllib`` only reads TOML,
so writes go through ``tomlkit``, which patches the parsed document in place and so keeps the user's
comments and formatting. ``settings.py`` still owns reads and validation; this module only writes.

When the file does not exist yet, the first write seeds it from the shipped example (``config init``'s
content) so a fresh file keeps its documentation rather than becoming a bare one-key stub.

Single-user app: writes are last-writer-wins. A hand-edit made while the app is also writing can be
clobbered; that is accepted rather than guarded with file locking.
"""

from __future__ import annotations

from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument

from kokua.config import file as settings
from kokua.mcp.servers import name_from_url


def _load(path: Path) -> TOMLDocument:
    """Parse the file for editing, seeding from the shipped example if it does not exist yet."""
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.parse(settings.example_text())


def _write(path: Path, doc: TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def set_value(path: Path, section: str, key: str, value) -> None:
    """Set ``[section].key = value``, creating the section if absent, preserving everything else."""
    doc = _load(path)
    table = doc.get(section)
    if table is None:
        table = tomlkit.table()
        doc[section] = table
    table[key] = value
    _write(path, doc)


def unset_value(path: Path, section: str, key: str) -> None:
    """Remove ``[section].key`` if present; a missing section or key is a no-op."""
    doc = _load(path)
    table = doc.get(section)
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

    Two servers on one host (a service exposing several MCP endpoints under one domain) derive the same
    base name; appending a numeric suffix until the name is free keeps a successful ``add_mcp_server``
    call from producing a config the registry's collision check would later reject at boot, deferred
    past the point where the tool could still report the problem usefully.
    """
    base = name_from_url(url)
    used = {entry.get("name") for entry in servers}
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


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
