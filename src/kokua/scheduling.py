"""Durable, agent-managed scheduled tasks.

AIMU's ``Scheduler`` runs in-memory jobs and is deliberately non-persistent; this module is the
"durable wrapper above the library" it defers to. It owns a tolerant JSON registry of tasks (mirroring
``mcp_registry.py``), the ``next_fire`` scheduler math for the supported recurrence types, and the
``make_scheduler_tools`` factory that binds the agent tools to the live ``Scheduler`` and the
assistant's proactive-turn method (mirroring ``mcp.make_mcp_tools``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def load(path: Path) -> list[dict]:
    """Return the persisted task records (``[]`` if the file is absent or unreadable)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read scheduled-task registry %s; ignoring it.", path, exc_info=True)
        return []
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, dict) and record.get("id")]


def add(path: Path, record: dict) -> None:
    """Append a task record, replacing any existing record with the same id."""
    records = [r for r in load(path) if r.get("id") != record["id"]]
    records.append(record)
    _write(path, records)


def remove(path: Path, task_id: str) -> bool:
    """Drop a task by id. Returns whether a record was actually removed."""
    records = load(path)
    kept = [r for r in records if r.get("id") != task_id]
    if len(kept) == len(records):
        return False
    _write(path, kept)
    return True


def find(records: list[dict], id_or_name: str) -> Optional[dict]:
    """Resolve a handle to a record, matching on id first, then name."""
    for record in records:
        if record.get("id") == id_or_name:
            return record
    for record in records:
        if record.get("name") == id_or_name:
            return record
    return None


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
