"""Per-conversation in-flight turn bookkeeping, replacing the single-turn fields on Assistant.

With concurrent per-conversation turns, the assistant tracks at most one running turn per
conversation (the per-conversation TurnGate lock enforces the "at most one"). This holds each turn's
RunHandle plus the diagnostics the /diag command and the front-end "working" indicator read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aimu.aio import RunHandle


@dataclass
class TurnInfo:
    handle: RunHandle
    started: float
    preview: str
    background: bool = False


class TurnTracker:
    def __init__(self):
        self._turns: dict[str, TurnInfo] = {}

    def add(self, conversation_id: str, info: TurnInfo) -> None:
        self._turns[conversation_id] = info

    def get(self, conversation_id: str) -> Optional[TurnInfo]:
        return self._turns.get(conversation_id)

    def remove(self, conversation_id: str) -> None:
        self._turns.pop(conversation_id, None)

    def remove_if(self, conversation_id: str, handle: RunHandle) -> None:
        """Remove ``conversation_id``'s entry only when it is the one holding ``handle``.

        A turn's done-callback must not evict a newer turn's entry for the same conversation. The gate
        should keep two live turns per conversation from ever coexisting, but this keeps the tracker
        correct even if one did: a finished turn only ever clears its own entry."""
        info = self._turns.get(conversation_id)
        if info is not None and info.handle is handle:
            del self._turns[conversation_id]

    def running(self, conversation_id: str) -> bool:
        info = self._turns.get(conversation_id)
        return info is not None and not info.handle.done

    def active_ids(self) -> list[str]:
        return list(self._turns.keys())

    def all(self) -> list[tuple[str, "TurnInfo"]]:
        return list(self._turns.items())
