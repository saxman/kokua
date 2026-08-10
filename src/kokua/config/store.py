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


def add_mcp_server(path: Path, url: str, token_env: str | None = None) -> None:
    """Append (or, by URL, replace) an ``[[mcp.server]]`` entry.

    A runtime-added server (OAuth or unauthenticated) is recorded with just its URL so it reconnects
    on the next restart; ``token_env`` is written only for a bearer-token server declared explicitly.
    """
    doc = _load(path)
    servers = _server_array(doc)
    for i, entry in enumerate(servers):
        if entry.get("url") == url:
            del servers[i]
            break
    table = tomlkit.table()
    table["url"] = url
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
