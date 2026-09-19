"""The human-decision slot: one pending request, whatever vocabulary the asker brings."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from kokua.config.schema import AssistantConfig, ReviewerConfig
from kokua.core.auto_approval import AutoApproval, Outcome, Review, ReviewContext, current_review_context
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
        self.auto_approvals: list[tuple[str, dict, bool, str, str]] = []
        self.alerts: list[str] = []

    async def ask_approval(self, name, arguments):
        self.asked.append(f"approve:{name}")

    async def show_auto_approval(self, name, arguments, *, approved, reason, model):
        self.auto_approvals.append((name, arguments, approved, reason, model))

    async def alert(self, text, *, conversation_id=None, group=None):
        self.alerts.append(text)


def _gate(ui):
    """A gate as the composition root leaves it: wired, and with its gated set already resolved.

    Startup resolves `[security].confirm_tools` (a list of `toolset.tool` entries) down to tool names
    and assigns them here, so a test builds the gate the same way rather than handing it config.
    """
    gate = HumanGate(ui, active_id=lambda: "c1", is_proactive=lambda: False, turn_conversation=lambda: "c1")
    gate.gated_tools = frozenset({"execute_python"})
    return gate


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


# --- the auto-approval review, and where it sits in the gate's order ----------------------------


def _auto() -> AutoApproval:
    """The gate `[security.auto_approval]` resolves to, built rather than resolved.

    Nothing on this path reads the reviewer beyond handing it to `review_call`, which every test here
    replaces, so a started assistant would only supply a tool vocabulary none of them consult.
    """
    config = AssistantConfig(reviewers={"approval": ReviewerConfig(model="ollama:b", system_message="judge it")})
    return AutoApproval(
        tools=frozenset({"run_command"}),
        toolset_of={"run_command": "compute"},
        reviewers=(config.reviewer_for("approval"),),
        timeout_seconds=10.0,
        max_per_turn=5,
    )


@pytest.fixture
def in_turn():
    """The budget a reactive turn opens, which `review_call` reads and fails closed without.

    Opened in every test here, including the ones asserting no review happens, so what those prove is
    the branch order rather than a missing context they would have failed closed on anyway.
    """
    token = current_review_context.set(ReviewContext(request="fix the failing test"))
    try:
        yield
    finally:
        current_review_context.reset(token)


def _answers(outcome: Outcome):
    """A stand-in for `review_call` giving `outcome`, so these tests exercise the wiring."""

    async def review(auto, *, tool, arguments):
        return outcome

    return review


def _never_called(why: str):
    def review(*args, **kwargs):
        raise AssertionError(why)

    return review


#: How long a helper here waits for the state it expects before calling the test failed. Generous
#: enough that a slow machine does not decide the outcome, and short enough that the failure arrives.
_SETTLE_TIMEOUT = 2.0


async def _answer_the_prompt(gate, name, arguments, *, with_answer: bool) -> bool:
    """Run `approve`, wait for the prompt it must raise, and route `with_answer` the way the loop does.

    The wait is bounded and polled rather than a fixed number of loop turns, because a review can sit
    in front of the prompt and how many turns that costs is not this helper's business. It fails
    rather than hanging when no prompt arrives, which is what a gate that answered for the user on its
    own would produce: nobody would resolve the request, and a suite that hangs says less than one
    that fails.
    """
    task = asyncio.create_task(gate.approve(name, arguments))
    try:
        async with asyncio.timeout(_SETTLE_TIMEOUT):
            # Slept rather than spun: on the failure path this polls for the whole timeout, and a busy
            # `sleep(0)` would burn a core for two seconds to reach an assertion that already failed.
            while not gate.approval.pending:
                await asyncio.sleep(0.001)
    except TimeoutError:
        task.cancel()
        # Awaited, so the cancellation lands before this returns: a task left pending is collected
        # later and prints "Task was destroyed but it is pending" over the failure being reported.
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise AssertionError(f"approve({name!r}) never stopped to ask") from None
    gate.approval.resolve(with_answer)
    return await task


async def _settle_without_a_prompt(gate, name, arguments) -> bool:
    """`approve`'s answer on a path that must not stop to ask, bounded so a regression fails.

    A plain `await` here would hang instead: these paths raise no prompt, so nothing in the test is
    waiting to answer one, and a gate that started asking would leave the request outstanding forever.
    """
    try:
        return await asyncio.wait_for(gate.approve(name, arguments), timeout=_SETTLE_TIMEOUT)
    except TimeoutError:
        raise AssertionError(f"approve({name!r}) stopped to ask instead of settling") from None


async def test_an_approved_review_needs_no_prompt(monkeypatch, in_turn):
    monkeypatch.setattr(
        "kokua.core.interaction.review_call", _answers(Outcome(True, "runs the test suite", "ollama:b"))
    )
    ui = _UI()
    gate = _gate(ui)
    gate.gated_tools = frozenset({"run_command"})
    gate.auto_approval = _auto()

    assert await _settle_without_a_prompt(gate, "run_command", {"command": "uv run pytest -q"}) is True

    assert ui.asked == []
    # Never silent: an auto-approval that replaced a visible prompt with an invisible decision is the
    # outcome this feature exists to avoid producing.
    assert ui.auto_approvals == [
        ("run_command", {"command": "uv run pytest -q"}, True, "runs the test suite", "ollama:b")
    ]


async def test_an_escalated_review_still_prompts_and_carries_the_reason(monkeypatch, in_turn):
    """A reviewer cannot deny, so what it withholds arrives at the prompt an ungated build would show."""
    monkeypatch.setattr("kokua.core.interaction.review_call", _answers(Outcome(False, "cannot be undone", "ollama:b")))
    ui = _UI()
    gate = _gate(ui)
    gate.gated_tools = frozenset({"run_command"})
    gate.auto_approval = _auto()

    assert await _answer_the_prompt(gate, "run_command", {"command": "rm -rf build"}, with_answer=False) is False

    assert ui.asked == ["approve:run_command"]
    assert ui.auto_approvals == [("run_command", {"command": "rm -rf build"}, False, "cannot be undone", "ollama:b")]


async def test_an_escalated_review_leaves_the_answer_to_the_user(monkeypatch, in_turn):
    """The prompt behind an escalation is the ordinary one, so a yes still runs the call."""
    monkeypatch.setattr("kokua.core.interaction.review_call", _answers(Outcome(False, "cannot be undone", "ollama:b")))
    gate = _gate(_UI())
    gate.gated_tools = frozenset({"run_command"})
    gate.auto_approval = _auto()

    assert await _answer_the_prompt(gate, "run_command", {"command": "rm -rf build"}, with_answer=True) is True


async def test_an_ineligible_gated_tool_is_never_reviewed(monkeypatch, in_turn):
    monkeypatch.setattr("kokua.core.interaction.review_call", _never_called("update_config must never be reviewed"))
    ui = _UI()
    gate = _gate(ui)
    gate.gated_tools = frozenset({"run_command", "update_config"})
    gate.auto_approval = _auto()  # its tools name run_command only

    assert await _answer_the_prompt(gate, "update_config", {"section": "security"}, with_answer=False) is False

    assert ui.asked == ["approve:update_config"]
    assert ui.auto_approvals == []


async def test_a_proactive_turn_is_never_reviewed(monkeypatch, in_turn):
    """An unattended turn has nobody to escalate to, so it is not offered a way to skip asking."""
    monkeypatch.setattr("kokua.core.interaction.review_call", _never_called("an unattended turn is not reviewable"))
    ui = _UI()
    gate = HumanGate(ui, active_id=lambda: "c1", is_proactive=lambda: True, turn_conversation=lambda: "c1")
    gate.gated_tools = frozenset({"run_command"})
    gate.auto_approval = _auto()

    assert await _settle_without_a_prompt(gate, "run_command", {"command": "ls"}) is False

    assert ui.asked == []
    assert ui.auto_approvals == []


async def test_a_turn_switched_away_from_is_never_reviewed(monkeypatch, in_turn):
    """Approving a call means reading it, and that turn is not on screen."""
    monkeypatch.setattr("kokua.core.interaction.review_call", _never_called("a backgrounded turn is not reviewable"))
    ui = _UI()
    gate = HumanGate(ui, active_id=lambda: "c1", is_proactive=lambda: False, turn_conversation=lambda: "elsewhere")
    gate.gated_tools = frozenset({"run_command"})
    gate.auto_approval = _auto()

    assert await _settle_without_a_prompt(gate, "run_command", {"command": "ls"}) is False

    assert ui.asked == []
    assert ui.auto_approvals == []
    assert len(ui.alerts) == 1  # the switched-away card, which says the call was denied


async def test_a_gate_with_no_auto_approval_prompts_as_before(monkeypatch, in_turn):
    """The feature ships off, and off means the gate behaves exactly as it did before it existed."""
    monkeypatch.setattr("kokua.core.interaction.review_call", _never_called("the feature is off"))
    ui = _UI()
    gate = _gate(ui)

    assert await _answer_the_prompt(gate, "execute_python", {"code": "1"}, with_answer=True) is True

    assert ui.asked == ["approve:execute_python"]
    assert ui.auto_approvals == []


class _ApprovingClient:
    """Stands in for the reviewer's own model client: only `chat(..., schema=...)` is ever called."""

    def __init__(self):
        self.default_generate_kwargs = {}

    async def chat(self, prompt, schema=None, use_tools=None):
        return Review(in_scope=True, reversible=True, injection_suspected=False, reason="runs the test suite")


async def test_an_approving_reviewer_removes_the_prompt_through_the_whole_chain(monkeypatch, in_turn):
    """The gate and the real `review_call` together, with nothing in this module replaced.

    Every test above substitutes `review_call` itself, which is what makes them read the wiring rather
    than the reviewer, and it leaves them unable to notice `review_call` changing shape underneath the
    call site. This one reaches the reviewer's client instead, the seam
    `tests/core/test_auto_approval.py` uses, so the gate's call has to still fit the function it names
    and the outcome's fields have to still reach the card.
    """
    monkeypatch.setattr("kokua.core.auto_approval._build_client", lambda reviewer: _ApprovingClient())
    ui = _UI()
    gate = _gate(ui)
    gate.gated_tools = frozenset({"run_command"})
    gate.auto_approval = _auto()

    assert await _settle_without_a_prompt(gate, "run_command", {"command": "uv run pytest -q"}) is True

    assert ui.asked == []
    assert ui.auto_approvals == [
        ("run_command", {"command": "uv run pytest -q"}, True, "runs the test suite", "ollama:b")
    ]
