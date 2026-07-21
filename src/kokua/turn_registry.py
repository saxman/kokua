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

    def running(self, conversation_id: str) -> bool:
        info = self._turns.get(conversation_id)
        return info is not None and not info.handle.done

    def active_ids(self) -> list[str]:
        return list(self._turns.keys())

    def all(self) -> list[tuple[str, "TurnInfo"]]:
        return list(self._turns.items())
