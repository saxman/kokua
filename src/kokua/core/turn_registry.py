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
    # The scheduled task this turn is a firing of, None for a turn the user asked for. Held here rather
    # than read back off the conversation's stored metadata so it is also known for a firing on a channel
    # with no conversation list, which runs in the viewed conversation and stamps no task id on it.
    task_id: Optional[str] = None


class TurnTracker:
    """One entry per conversation, plus the set of every turn actually still running.

    The two answer different questions and cannot be the same structure. ``/stop``, the working
    indicator and ``/diag`` want *the* turn for a conversation, so those read a map that holds one; but
    a turn submitted while another is still running on that conversation replaces the entry without
    ending the turn it replaced, so that map is not the list of what is in flight. Shutdown needs the
    list, since it closes the session store and a turn still running when that happens dies part way
    through the provenance record it makes on its way down.
    """

    def __init__(self):
        self._turns: dict[str, TurnInfo] = {}
        # A list compared by identity rather than a set, so nothing here depends on ``RunHandle`` being
        # hashable. It is a foreign type, its hashability is incidental to being a plain class, and a
        # set would turn a change to it into a runtime failure at the moment a turn starts. At most a
        # handful of turns are ever in flight, so the linear removal below costs nothing.
        self._live: list[RunHandle] = []

    def add(self, conversation_id: str, info: TurnInfo) -> None:
        self._turns[conversation_id] = info
        self._live.append(info.handle)

    def get(self, conversation_id: str) -> Optional[TurnInfo]:
        return self._turns.get(conversation_id)

    def remove_if(self, conversation_id: str, handle: RunHandle) -> None:
        """Remove ``conversation_id``'s entry only when it is the one holding ``handle``.

        A turn's done-callback must not evict a newer turn's entry for the same conversation. The gate
        serializes same-conversation turns but does not stop a second one being *submitted*, so two can
        coexist with only the newer one holding the entry: a finished turn therefore only ever clears
        its own.

        The live list drops the handle unconditionally, keyed by the handle rather than by the
        conversation, precisely because a displaced turn no longer matches the entry. Leaving it behind
        would make shutdown wait on a turn that has already ended."""
        self._live = [live for live in self._live if live is not handle]
        info = self._turns.get(conversation_id)
        if info is not None and info.handle is handle:
            del self._turns[conversation_id]

    def running(self, conversation_id: str) -> bool:
        info = self._turns.get(conversation_id)
        return info is not None and not info.handle.done

    def all(self) -> list[tuple[str, "TurnInfo"]]:
        return list(self._turns.items())

    def live(self) -> list[RunHandle]:
        """Every turn still running, including one whose entry a later turn on the same conversation
        replaced.

        This is what shutdown cancels and waits for. Reading ``all()`` there instead would miss a
        displaced turn, which the event loop then cancels after the session store has closed, and the
        record it makes while unwinding raises out of a task nobody is watching."""
        return [handle for handle in self._live if not handle.done]

    def for_task(self, task_id: str) -> list[tuple[str, "TurnInfo"]]:
        """Every still-running turn that is a firing of ``task_id``, as ``(conversation id, info)``.

        A list rather than one entry: a task can have two firings in flight at once (a manual run-now
        alongside its armed one), each in a conversation of its own, so stopping "the task's run" means
        stopping all of them."""
        return [
            (conversation_id, info)
            for conversation_id, info in self._turns.items()
            if info.task_id == task_id and not info.handle.done
        ]
