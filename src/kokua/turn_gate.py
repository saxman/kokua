"""A readers-writer gate for per-conversation turns vs. exclusive config mutations.

Turns are "readers": many run concurrently, but at most one per conversation (they share that
conversation's agent + message list, so same-conversation turns must serialize). Config mutations
(model switch, generation-settings changes) are the "writer": they touch every agent, so they run
exclusively, waiting for in-flight turns to drain and blocking new ones until they finish.

Writer-preferring: once an exclusive hold is waiting, new turns queue behind it so a steady stream of
turns can't starve a settings change.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Callable


class TurnGate:
    def __init__(self, lock_for: Callable[[str], asyncio.Lock]):
        self._lock_for = lock_for
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer_waiting = 0
        self._writer_active = False

    @asynccontextmanager
    async def turn(self, conversation_id: str):
        async with self._cond:
            while self._writer_active or self._writer_waiting:
                await self._cond.wait()
            self._readers += 1
        per_conversation = self._lock_for(conversation_id)
        await per_conversation.acquire()
        try:
            yield
        finally:
            per_conversation.release()
            async with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @asynccontextmanager
    async def exclusive(self):
        async with self._cond:
            self._writer_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    await self._cond.wait()
                self._writer_active = True
            finally:
                self._writer_waiting -= 1
        try:
            yield
        finally:
            async with self._cond:
                self._writer_active = False
                self._cond.notify_all()

    def active_turns(self) -> int:
        return self._readers
