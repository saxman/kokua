"""Human-in-the-loop decisions: tool approval, and whatever a workflow needs to ask.

Both are the same shape -- a turn stops, asks the user something over the channel, and waits; the
serve loop reads the next inbound message and resolves the wait. ``PendingRequest`` is that shape,
once. Tool approval fixes its own vocabulary (y/n) because the core owns it; a workflow's decision
brings its own parser, so no one workflow's reply words live here.

**Every pending request is single-slot and lock-guarded.** Concurrent turns (or concurrent tool
calls within one turn) would otherwise both write the slot the serve loop resolves, and the first
waiter would be resolved with the second's answer or left hanging forever. The lock makes a second
asker wait until the first has been answered and the slot cleared.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_UNSET = object()  # distinguishes "no default given for this ask" from a legitimate default of None


class PendingRequest(Generic[T]):
    """One outstanding human decision.

    ``default`` is the answer used when the request is abandoned (the user navigated away from the
    turn that raised it) -- deny for approval, or whichever safe answer the current asker chose for a
    workflow decision. Abandoning rather than leaving it pending is what keeps a backgrounded turn
    from waiting forever, and keeps the next message the user types from being misrouted into it.
    """

    def __init__(self, default: T):
        self._default = default
        self._lock = asyncio.Lock()
        self._future: Optional[asyncio.Future] = None
        self._context: Any = None
        self._parse: Optional[Callable[[str, str], T]] = None

    @property
    def pending(self) -> bool:
        return self._future is not None and not self._future.done()

    @property
    def context(self) -> Any:
        """Whatever the asker attached for the resolver to read (the plan text under review)."""
        return self._context

    async def ask(
        self,
        prompt: Callable[[], Awaitable[None]],
        *,
        context: Any = None,
        parse: Optional[Callable[[str, str], T]] = None,
        default: Any = _UNSET,
    ) -> T:
        """Send ``prompt`` and wait for the answer, serialized against any other asker.

        ``parse`` turns the user's reply into the answer, and is the asker's rather than this class's:
        a workflow's vocabulary belongs to the workflow. ``default`` (if given) overrides the answer an
        abandoned request resolves with, for this ask only: it is set under the same lock that guards
        the rest of this ask's state and restored in the ``finally``, so an overlapping second asker's
        default can never leak onto the first asker's abandon. The slot is cleared in that same
        ``finally``, so a ``/stop`` that cancels the waiting turn mid-await still leaves no stale
        pending request behind.
        """
        async with self._lock:
            previous_default = self._default
            if default is not _UNSET:
                self._default = default
            self._future = asyncio.get_running_loop().create_future()
            self._context = context
            self._parse = parse
            try:
                await prompt()
                return await self._future
            finally:
                self._default = previous_default
                self._future = None
                self._context = None
                self._parse = None

    def resolve(self, value: T) -> bool:
        """Answer the outstanding request. Returns whether there was one to answer."""
        if not self.pending:
            return False
        self._future.set_result(value)
        return True

    def parse_reply(self, raw: str, text: str) -> T:
        """The outstanding request's answer for this reply, via the asker's parser (raw text if none)."""
        if self._parse is None:
            return raw
        return self._parse(raw, text)

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
        # One slot for whatever the running workflow asks. Single-slot and lock-guarded like approval:
        # a second asker waits until the first is answered, so the serve loop can never resolve the
        # wrong waiter.
        self.decision: PendingRequest[Any] = PendingRequest(default=None)

    def abandon_all(self) -> None:
        """Resolve any pending approval or workflow decision as denied/rejected.

        Called before switching the viewed conversation away from the turn that raised them: that
        turn keeps running in the background (switching does not cancel it), so without this its
        awaited future would hang forever, and a reply the user types after switching could
        otherwise be misrouted to it instead of starting a new turn.
        """
        self.approval.abandon()
        self.decision.abandon()

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

    async def decide(
        self,
        prompt: Callable[[], Awaitable[None]],
        parse: Callable[[str, str], Any],
        *,
        default: Any = None,
        context: Any = None,
    ) -> Any:
        """Ask the user something on a workflow's behalf and wait for the answer.

        ``default`` is what an abandoned request answers with (the user navigated away from the turn
        that raised it). It is passed per call rather than fixed at construction because it is the
        workflow's safe answer, not the core's: "reject" for a plan review, but a different word for
        the next workflow along.
        """
        return await self.decision.ask(prompt, context=context, parse=parse, default=default)

    def resolve_reply(self, raw: str, text: str) -> bool:
        """Route an inbound message to whichever request is outstanding. Returns whether it was consumed.

        ``text`` is the lowercased, stripped form; ``raw`` preserves the user's casing, which an edited
        plan needs. Approval takes precedence: the two are never outstanding at once in practice, and
        checking in a fixed order keeps that assumption from mattering.

        A decision's parser is the asker's code, not the core's, so it can raise on a reply it did not
        expect (a plugin workflow's parser is by design arbitrary). Left unguarded that would propagate
        out of the serve loop and take the whole assistant down over one bad reply; caught here, the
        request is abandoned (answered with its own default) and the loop keeps serving.
        """
        if self.approval.pending:
            return self.approval.resolve(text in ("y", "yes"))
        if self.decision.pending:
            try:
                answer = self.decision.parse_reply(raw, text)
            except Exception:
                logger.warning("Workflow decision parser failed; answering with its default", exc_info=True)
                self.decision.abandon()
                return True
            return self.decision.resolve(answer)
        return False
