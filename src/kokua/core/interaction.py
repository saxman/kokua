"""Human-in-the-loop decisions: tool approval and plan review.

Both are the same shape -- a turn stops, asks the user something over the channel, and waits; the
serve loop reads the next inbound message and resolves the wait. ``PendingRequest`` is that shape,
once, and the two concrete requests differ only in their reply vocabulary and their default answer.

**Every pending request is single-slot and lock-guarded.** Concurrent turns (or concurrent tool
calls within one turn) would otherwise both write the slot the serve loop resolves, and the first
waiter would be resolved with the second's answer or left hanging forever. The lock makes a second
asker wait until the first has been answered and the slot cleared.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


class PendingRequest(Generic[T]):
    """One outstanding human decision.

    ``default`` is the answer used when the request is abandoned (the user navigated away from the
    turn that raised it) -- deny for approval, reject for plan review. Abandoning rather than leaving
    it pending is what keeps a backgrounded turn from waiting forever, and keeps the next message the
    user types from being misrouted into it.
    """

    def __init__(self, default: T):
        self._default = default
        self._lock = asyncio.Lock()
        self._future: Optional[asyncio.Future] = None
        self._context: Any = None

    @property
    def pending(self) -> bool:
        return self._future is not None and not self._future.done()

    @property
    def context(self) -> Any:
        """Whatever the asker attached for the resolver to read (the plan text under review)."""
        return self._context

    async def ask(self, prompt: Callable[[], Awaitable[None]], *, context: Any = None) -> T:
        """Send ``prompt`` and wait for the answer, serialized against any other asker.

        The slot is cleared in a ``finally``, so a ``/stop`` that cancels the waiting turn mid-await
        (raising CancelledError out of it) still leaves no stale pending request behind.
        """
        async with self._lock:
            self._future = asyncio.get_running_loop().create_future()
            self._context = context
            try:
                await prompt()
                return await self._future
            finally:
                self._future = None
                self._context = None

    def resolve(self, value: T) -> bool:
        """Answer the outstanding request. Returns whether there was one to answer."""
        if not self.pending:
            return False
        self._future.set_result(value)
        return True

    def abandon(self) -> None:
        """Resolve with the default answer, if outstanding."""
        self.resolve(self._default)


class HumanGate:
    """The assistant's two human-decision points, and how a channel reply routes to them."""

    def __init__(
        self,
        ui,
        config,
        *,
        active_id: Callable[[], str],
        is_proactive: Callable[[], bool],
        turn_conversation: Callable[[], Optional[str]],
    ):
        self._ui = ui
        self._config = config
        self._active_id = active_id
        self._is_proactive = is_proactive
        self._turn_conversation = turn_conversation
        self.approval: PendingRequest[bool] = PendingRequest(default=False)
        self.plan: PendingRequest[Optional[str]] = PendingRequest(default=None)

    def abandon_all(self) -> None:
        """Resolve any pending approval or plan review as denied/rejected.

        Called before switching the viewed conversation away from the turn that raised them: that
        turn keeps running in the background (switching does not cancel it), so without this its
        awaited future would hang forever, and a reply the user types after switching could
        otherwise be misrouted to it instead of starting a new turn.
        """
        self.approval.abandon()
        self.plan.abandon()

    async def approve(self, name: str, arguments: dict) -> bool:
        """Tool-approval gate run before each tool call (published to the model client per run).

        Ungated tools pass. A proactive/scheduled turn always auto-denies a gated tool: it is
        unattended, so nobody is watching to confirm, and a ``target="active"`` scheduled task would
        otherwise look foreground (its turn conversation equals the viewed one) and wrongly prompt.
        Otherwise a reactive turn is approved only if its conversation is the one the user is
        currently viewing; a turn backgrounded by a switch auto-denies. Otherwise prompt over the
        channel and await the answer, which the serve loop routes here.
        """
        if name not in self._config.confirm_tools:
            return True
        if self._is_proactive():
            return False
        if self._turn_conversation() != self._active_id():
            return False
        return await self.approval.ask(lambda: self._ui.ask_approval(name, arguments))

    async def review_plan(self, plan_text: str, critique: Optional[list[str]] = None) -> Optional[str]:
        """Await the user's decision on a plan: approve (the plan), edit (their text), or reject (None).

        Any adversarial-reviewer critique is surfaced with the prompt so the human can weigh it. The
        plan text rides along as the request's context, so ``resolve_reply`` can answer "approve"
        with the plan the user was actually looking at.
        """
        return await self.plan.ask(
            lambda: self._ui.ask_plan_review(plan_text, critique),
            context=plan_text,
        )

    def resolve_reply(self, raw: str, text: str) -> bool:
        """Route an inbound message to whichever request is outstanding. Returns whether it was consumed.

        ``text`` is the lowercased, stripped form; ``raw`` preserves the user's casing, which an
        edited plan needs. Approval takes precedence: the two are never outstanding at once in
        practice (a plan review happens before the executor runs any tool), and checking in a fixed
        order keeps that assumption from mattering.
        """
        if self.approval.pending:
            return self.approval.resolve(text in ("y", "yes"))
        if self.plan.pending:
            return self.plan.resolve(self._parse_plan_reply(raw, text))
        return False

    def _parse_plan_reply(self, raw: str, text: str) -> Optional[str]:
        """approve -> the reviewed plan, reject -> None, anything else -> the user's edited plan."""
        if text in ("approve", "yes", "y"):
            return self.plan.context
        if text in ("reject", "no", "n"):
            return None
        if text.startswith("edit:"):
            return raw.split(":", 1)[1].strip() or self.plan.context
        return raw.strip()
