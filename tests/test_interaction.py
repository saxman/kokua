"""PendingRequest: the single-slot, lock-guarded human-decision primitive."""

from __future__ import annotations

import asyncio

from kokua.interaction import PendingRequest


async def _noop() -> None:
    pass


async def test_ask_returns_the_routed_answer():
    request: PendingRequest[bool] = PendingRequest(default=False)
    asking = asyncio.create_task(request.ask(_noop))
    await asyncio.sleep(0)
    assert request.pending
    assert request.resolve(True) is True
    assert await asking is True
    assert not request.pending  # the slot is cleared for the next asker


def test_resolve_without_an_outstanding_request_is_a_no_op():
    request: PendingRequest[bool] = PendingRequest(default=False)
    assert request.resolve(True) is False


async def test_abandon_answers_with_the_default():
    request: PendingRequest[str] = PendingRequest(default="rejected")
    asking = asyncio.create_task(request.ask(_noop))
    await asyncio.sleep(0)
    request.abandon()
    assert await asking == "rejected"


async def test_context_rides_along_and_is_cleared():
    request: PendingRequest[str] = PendingRequest(default="")
    asking = asyncio.create_task(request.ask(_noop, context="the plan under review"))
    await asyncio.sleep(0)
    assert request.context == "the plan under review"
    request.resolve(request.context)
    assert await asking == "the plan under review"
    assert request.context is None


async def test_cancelling_the_waiter_leaves_no_stale_slot():
    """A `/stop` cancels the turn mid-await; the next asker must find a clean slot."""
    request: PendingRequest[bool] = PendingRequest(default=False)
    asking = asyncio.create_task(request.ask(_noop))
    await asyncio.sleep(0)
    asking.cancel()
    await asyncio.gather(asking, return_exceptions=True)
    assert not request.pending

    again = asyncio.create_task(request.ask(_noop))
    await asyncio.sleep(0)
    assert request.resolve(True) is True
    assert await again is True


async def test_concurrent_askers_are_serialized_and_neither_slot_is_clobbered():
    """The regression this class exists for.

    Two concurrent turns (or two concurrent gated tool calls in one turn) both ask. Without the
    lock, the second overwrites the slot the first is waiting on: the first hangs forever and the
    resolver's answer lands on the wrong request. With it, the second asker does not even create its
    future until the first has been answered and the slot cleared.

    The plan-review slot lacked this guard until PendingRequest unified the two; only the approval
    slot had it.
    """
    request: PendingRequest[str] = PendingRequest(default="")
    answered: list[str] = []

    async def ask(tag: str) -> None:
        async def prompt() -> None:
            # Yield so the two askers interleave at exactly the point the bug needed.
            await asyncio.sleep(0)
            assert request.context == tag  # the slot in play is this asker's own
            request.resolve(tag)

        answered.append(await request.ask(prompt, context=tag))

    await asyncio.wait_for(asyncio.gather(ask("a"), ask("b")), timeout=2.0)
    assert sorted(answered) == ["a", "b"]  # each asker got its own answer, not the other's
