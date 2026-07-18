"""Per-conversation agent cache: lazy build, LRU eviction, per-conversation locks.

Replaces the assistant's single shared agent. Each conversation gets its own agent (own model
client + message list), built on first access by an injected factory and evicted least-recently-used
when the cache exceeds its cap. An evicted agent simply rebuilds from persisted state on next
access, so the cap bounds memory, not correctness; this phase has no caller running a turn on an
evicted agent, since the global turn lock serializes turns and recency keeps the foreground
conversation's agent live. `pin`/`unpin` are reference-counted eviction guards provided for a later
phase (concurrent per-conversation turns), where an in-flight turn must survive across the cap even
if its conversation isn't the foreground one; unused so far.

Pure and Assistant-agnostic: it owns the mapping and its lifecycle; the caller owns what an agent is
and how it is built (the `build` callable) and run.
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from typing import Any, Callable


class AgentRegistry:
    """Maps conversation id -> agent, building lazily and evicting LRU (never an in-use agent)."""

    def __init__(self, build: Callable[[str], Any], *, cap: int = 8):
        self._build = build
        self._cap = max(1, cap)
        self._agents: "OrderedDict[str, Any]" = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._pins: Counter = Counter()

    def get(self, conversation_id: str) -> Any:
        """Return the conversation's agent, building it on a cache miss. Marks it most-recently-used."""
        agent = self._agents.get(conversation_id)
        if agent is None:
            agent = self._build(conversation_id)
            self._agents[conversation_id] = agent
        self._agents.move_to_end(conversation_id)
        self._evict()
        return agent

    def lock(self, conversation_id: str) -> asyncio.Lock:
        """A per-conversation lock, created on first request and stable thereafter."""
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def pin(self, conversation_id: str) -> None:
        """Protect a conversation's agent from eviction (reference-counted; pair with unpin)."""
        self._pins[conversation_id] += 1

    def unpin(self, conversation_id: str) -> None:
        """Release one eviction guard placed by pin."""
        if self._pins[conversation_id] <= 1:
            del self._pins[conversation_id]
        else:
            self._pins[conversation_id] -= 1

    def discard(self, conversation_id: str) -> None:
        """Drop a conversation's cached agent and lock (e.g. on delete)."""
        self._agents.pop(conversation_id, None)
        self._locks.pop(conversation_id, None)
        self._pins.pop(conversation_id, None)

    def live_agents(self) -> list[Any]:
        """The currently-cached agent instances (for fan-out of global mutations)."""
        return list(self._agents.values())

    def cached_ids(self) -> list[str]:
        """Cached conversation ids, least-recently-used first."""
        return list(self._agents.keys())

    def _evict(self) -> None:
        """Evict least-recently-used, unpinned agents until unpinned agent count <= cap."""
        for conversation_id in list(self._agents.keys()):
            unpinned_count = sum(1 for c in self._agents if not self._pins.get(c))
            if unpinned_count <= self._cap:
                return
            if self._pins.get(conversation_id):
                continue
            del self._agents[conversation_id]
