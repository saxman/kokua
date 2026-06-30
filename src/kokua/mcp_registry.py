"""Persist the set of runtime-added remote MCP servers so they reconnect across restarts.

The ``add_mcp_server`` chat tool connects a server mid-session; without this its tools are lost on
restart (the boot path only reconnects servers declared in config). This stores the minimal record
needed to reconnect: the URL and an auth mode the assistant can reproduce without a stored secret
(``"none"`` = unauthenticated, ``"oauth"`` = the persisted-token OAuth flow). Bearer-token servers are
**not** recorded (their secret would have to live on disk in plaintext); persist those via
``config.toml`` ``[mcp]`` instead.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Auth modes that can be reconnected at boot without a stored secret.
RECONNECTABLE = ("none", "oauth")


def load(path: Path) -> list[dict]:
    """Return the persisted server records (``[]`` if the file is absent or unreadable)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read MCP server registry %s; ignoring it.", path, exc_info=True)
        return []
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, dict) and record.get("url")]


def add(path: Path, url: str, auth_mode: str) -> None:
    """Record (or update) a reconnectable server by URL. Non-reconnectable modes are ignored."""
    if auth_mode not in RECONNECTABLE:
        return
    records = [record for record in load(path) if record.get("url") != url]
    records.append({"url": url, "auth_mode": auth_mode})
    _write(path, records)


def remove(path: Path, url: str) -> bool:
    """Drop a server by URL. Returns whether a record was actually removed."""
    records = load(path)
    kept = [record for record in records if record.get("url") != url]
    if len(kept) == len(records):
        return False
    _write(path, kept)
    return True


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
