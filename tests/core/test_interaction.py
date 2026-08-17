"""The human-decision slot: one pending request, whatever vocabulary the asker brings."""

from __future__ import annotations

import asyncio

from kokua.core.interaction import HumanGate, PendingRequest


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


async def test_a_second_askers_default_does_not_leak_onto_the_first():
    """The race `default` is set and restored under the lock to prevent.

    A starts an ask with its own default and is still inside its prompt (holding the lock) when B's
    ask is created and blocks acquiring that same lock. If B's default took effect as soon as `ask` is
    called, rather than once B actually holds the slot, an abandon at this point would answer A's
    waiter with B's safe answer instead of A's own -- exactly what a conversation switch's
    `abandon_all()` does on every overlap, not just a contrived one.
    """
    request: PendingRequest[str] = PendingRequest(default="unset")
    a_is_prompting = asyncio.Event()
    release_a = asyncio.Event()

    async def prompt_a() -> None:
        a_is_prompting.set()
        await release_a.wait()

    a = asyncio.create_task(request.ask(prompt_a, default="a-default"))
    await a_is_prompting.wait()
    assert request.pending

    b = asyncio.create_task(request.ask(_noop, default="b-default"))
    await asyncio.sleep(0)  # B blocks acquiring the lock; it must not touch `_default` yet

    request.abandon()  # only A's request occupies the slot right now
    release_a.set()
    assert await a == "a-default"  # not "b-default"

    await asyncio.sleep(0)  # let B acquire the now-free lock and register its own future
    assert request.pending
    request.abandon()
    assert await b == "b-default"


class _UI:
    def __init__(self):
        self.asked: list[str] = []

    async def ask_approval(self, name, arguments):
        self.asked.append(f"approve:{name}")


class _Config:
    confirm_tools = ["execute_python"]


def _gate(ui):
    return HumanGate(ui, _Config(), active_id=lambda: "c1", is_proactive=lambda: False, turn_conversation=lambda: "c1")


async def test_a_decision_uses_the_askers_own_parser():
    ui = _UI()
    gate = _gate(ui)

    async def prompt():
        ui.asked.append("decide")

    task = asyncio.create_task(gate.decide(prompt, lambda raw, text: text.upper(), default=None, context="ctx"))
    await asyncio.sleep(0)
    assert gate.decision.pending
    assert gate.resolve_reply("yep", "yep") is True
    assert await task == "YEP"


async def test_the_parser_can_read_the_context_it_was_given():
    gate = _gate(_UI())

    async def prompt():
        return None

    task = asyncio.create_task(
        gate.decide(prompt, lambda raw, text: gate.decision.context, default=None, context="the plan")
    )
    await asyncio.sleep(0)
    gate.resolve_reply("approve", "approve")
    assert await task == "the plan"


async def test_abandoning_a_decision_answers_with_its_default():
    gate = _gate(_UI())

    async def prompt():
        return None

    task = asyncio.create_task(gate.decide(prompt, lambda raw, text: "parsed", default="fallback"))
    await asyncio.sleep(0)
    gate.abandon_all()
    assert await task == "fallback"


async def test_approval_takes_precedence_over_a_waiting_decision():
    gate = _gate(_UI())

    async def prompt():
        return None

    decision = asyncio.create_task(gate.decide(prompt, lambda raw, text: "decided", default=None))
    await asyncio.sleep(0)
    approval = asyncio.create_task(gate.approve("execute_python", {}))
    await asyncio.sleep(0)

    assert gate.resolve_reply("y", "y") is True
    assert await approval is True
    gate.abandon_all()
    assert await decision is None


async def test_a_raising_parser_abandons_the_decision_instead_of_crashing_the_serve_loop():
    """A workflow's parser is arbitrary code (by design, that includes third-party plugins), so it can
    raise on a reply it did not expect. Before decisions had their own parser, the code running here
    was core-owned string matching that could not raise; `resolve_reply` now guards the call so one bad
    reply abandons that decision rather than propagating out of the serve loop and killing the process.
    """
    gate = _gate(_UI())

    async def prompt():
        return None

    def bad_parser(raw, text):
        raise ValueError("does not understand this reply")

    task = asyncio.create_task(gate.decide(prompt, bad_parser, default="safe-default"))
    await asyncio.sleep(0)
    assert gate.resolve_reply("whatever", "whatever") is True  # consumed, not propagated
    assert await task == "safe-default"
    assert not gate.decision.pending  # the slot is clean for the next decision

    # The gate keeps serving: a later decision with a well-behaved parser works normally.
    followup = asyncio.create_task(gate.decide(prompt, lambda raw, text: text.upper(), default=None))
    await asyncio.sleep(0)
    assert gate.resolve_reply("ok", "ok") is True
    assert await followup == "OK"
