"""Unit tests for TurnGate: per-conversation turn exclusion, cross-conversation concurrency,
exclusive config holds."""

from __future__ import annotations

import asyncio

from kokua.turn_gate import TurnGate


def _gate():
    locks: dict[str, asyncio.Lock] = {}

    def lock_for(cid):
        return locks.setdefault(cid, asyncio.Lock())

    return TurnGate(lock_for)


async def test_same_conversation_turns_serialize():
    gate = _gate()
    order = []

    async def turn(tag, hold):
        async with gate.turn("c1"):
            order.append(f"{tag}-start")
            await asyncio.sleep(hold)
            order.append(f"{tag}-end")

    await asyncio.gather(turn("a", 0.02), turn("b", 0.0))
    # b cannot start until a ends (same conversation).
    assert order == ["a-start", "a-end", "b-start", "b-end"]


async def test_different_conversations_run_concurrently():
    gate = _gate()
    started = []
    release = asyncio.Event()

    async def turn(cid):
        async with gate.turn(cid):
            started.append(cid)
            await release.wait()

    t1 = asyncio.create_task(turn("c1"))
    t2 = asyncio.create_task(turn("c2"))
    await asyncio.sleep(0.01)
    assert set(started) == {"c1", "c2"}  # both entered concurrently
    release.set()
    await asyncio.gather(t1, t2)


async def test_exclusive_waits_for_active_turns_and_blocks_new_ones():
    gate = _gate()
    events = []
    turn_holding = asyncio.Event()
    let_turn_finish = asyncio.Event()

    async def turn():
        async with gate.turn("c1"):
            turn_holding.set()
            events.append("turn-in")
            await let_turn_finish.wait()
            events.append("turn-out")

    async def writer():
        await turn_holding.wait()
        async with gate.exclusive():
            events.append("writer-in")
            events.append("writer-out")

    async def late_turn():
        await turn_holding.wait()
        await asyncio.sleep(0.01)  # arrives while writer is waiting
        async with gate.turn("c2"):
            events.append("late-turn")

    t = asyncio.create_task(turn())
    w = asyncio.create_task(writer())
    lt = asyncio.create_task(late_turn())
    await asyncio.sleep(0.02)
    let_turn_finish.set()
    await asyncio.gather(t, w, lt)
    # writer runs only after the active turn drains; the late turn waits for the writer.
    assert events.index("turn-out") < events.index("writer-in")
    assert events.index("writer-out") < events.index("late-turn")


async def test_active_turns_count():
    gate = _gate()
    assert gate.active_turns() == 0
    rel = asyncio.Event()

    async def turn(cid):
        async with gate.turn(cid):
            await rel.wait()

    t1 = asyncio.create_task(turn("a"))
    t2 = asyncio.create_task(turn("b"))
    await asyncio.sleep(0.01)
    assert gate.active_turns() == 2
    rel.set()
    await asyncio.gather(t1, t2)
    assert gate.active_turns() == 0


async def test_cancelled_turn_reverts_reader_count():
    """Regression: ensure that cancelling a turn while awaiting per-conversation lock
    reverts the reader count. If not, exclusive() will deadlock waiting for readers == 0."""
    gate = _gate()
    turn_a_entered = asyncio.Event()
    let_turn_a_exit = asyncio.Event()

    async def turn_a():
        async with gate.turn("c1"):
            turn_a_entered.set()
            await let_turn_a_exit.wait()

    async def turn_b():
        async with gate.turn("c1"):
            pass  # will block on per-conversation lock held by turn_a

    # Start turn A (enters successfully, holds the per-conversation lock).
    t_a = asyncio.create_task(turn_a())
    await turn_a_entered.wait()
    assert gate.active_turns() == 1

    # Start turn B (increments readers to 2, but blocks on per-conversation lock).
    t_b = asyncio.create_task(turn_b())
    await asyncio.sleep(0.01)
    assert gate.active_turns() == 2

    # Cancel turn B while it's blocked on the lock acquisition.
    t_b.cancel()
    try:
        await t_b
    except asyncio.CancelledError:
        pass

    # Turn B should have reverted its reader count on cancellation.
    await asyncio.sleep(0.01)
    assert gate.active_turns() == 1

    # Let turn A exit.
    let_turn_a_exit.set()
    await t_a

    # Reader count should be back to 0.
    assert gate.active_turns() == 0

    # exclusive() should complete without hanging (no deadlock).
    async with asyncio.timeout(1.0):
        async with gate.exclusive():
            pass
