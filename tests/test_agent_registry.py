"""Unit tests for AgentRegistry: lazy build, LRU eviction, in-use pinning, per-conversation locks."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from kokua.agent_registry import AgentRegistry


def _registry(cap=8):
    built = []

    def build(conversation_id):
        agent = MagicMock(name=f"agent:{conversation_id}")
        agent.conversation_id = conversation_id
        built.append(conversation_id)
        return agent

    return AgentRegistry(build, cap=cap), built


def test_get_builds_once_and_caches():
    registry, built = _registry()
    a1 = registry.get("c1")
    a2 = registry.get("c1")
    assert a1 is a2
    assert built == ["c1"]


def test_get_builds_distinct_agents_per_conversation():
    registry, built = _registry()
    assert registry.get("c1") is not registry.get("c2")
    assert built == ["c1", "c2"]


def test_lru_eviction_over_cap_rebuilds_on_reaccess():
    registry, built = _registry(cap=2)
    registry.get("c1")
    registry.get("c2")
    registry.get("c3")  # evicts c1 (least recently used)
    assert registry.cached_ids() == ["c2", "c3"]
    registry.get("c1")  # rebuilt
    assert built == ["c1", "c2", "c3", "c1"]


def test_access_refreshes_recency():
    registry, _ = _registry(cap=2)
    registry.get("c1")
    registry.get("c2")
    registry.get("c1")  # c1 now MRU
    registry.get("c3")  # evicts c2, not c1
    assert set(registry.cached_ids()) == {"c1", "c3"}


def test_pinned_agent_is_never_evicted():
    registry, _ = _registry(cap=1)
    registry.get("c1")
    registry.pin("c1")
    registry.get("c2")  # cap=1 but c1 is pinned, so cache holds both
    assert set(registry.cached_ids()) == {"c1", "c2"}
    registry.unpin("c1")
    registry.get("c3")  # now c1 (LRU, unpinned) may be evicted
    assert "c1" not in registry.cached_ids()


def test_pin_is_reference_counted():
    registry, _ = _registry(cap=1)
    registry.get("c1")
    registry.pin("c1")
    registry.pin("c1")
    registry.unpin("c1")
    registry.get("c2")
    assert "c1" in registry.cached_ids()  # still pinned once


def test_lock_is_stable_per_conversation():
    registry, _ = _registry()
    lock = registry.lock("c1")
    assert registry.lock("c1") is lock
    assert isinstance(lock, asyncio.Lock)
    assert registry.lock("c2") is not lock


def test_discard_drops_agent_and_lock():
    registry, built = _registry()
    lock = registry.lock("c1")
    registry.get("c1")
    registry.discard("c1")
    assert registry.cached_ids() == []
    registry.get("c1")  # rebuilt after discard
    assert built == ["c1", "c1"]
    assert registry.lock("c1") is not lock  # the old lock was dropped, not reused


def test_live_agents_returns_cached_instances():
    registry, _ = _registry()
    a1 = registry.get("c1")
    a2 = registry.get("c2")
    assert set(registry.live_agents()) == {a1, a2}
