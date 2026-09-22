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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Generic, Optional, TypeVar

from kokua.core.auto_approval import AutoApproval, review_call

if TYPE_CHECKING:
    from kokua.core.approval import ApprovalPolicies

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
        retire: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> T:
        """Send ``prompt`` and wait for the answer, serialized against any other asker.

        ``parse`` turns the user's reply into the answer, and is the asker's rather than this class's:
        a workflow's vocabulary belongs to the workflow. ``default`` (if given) overrides the answer an
        abandoned request resolves with, for this ask only: it is set under the same lock that guards
        the rest of this ask's state and restored in the ``finally``, so an overlapping second asker's
        default can never leak onto the first asker's abandon. The slot is cleared in that same
        ``finally``, so a ``/stop`` that cancels the waiting turn mid-await still leaves no stale
        pending request behind.

        ``retire`` takes back what ``prompt`` put up, and runs once this request is no longer
        outstanding *however* it stopped being outstanding: answered, abandoned by a switch, or
        cancelled with the turn that raised it. It is the asker's too, and it is paired with ``prompt``
        rather than left to the answering paths because there are four of those and every one of them
        leaves the same live question on a page that has no other way to learn it is closed. A front
        end whose prompt is a printed line has nothing to take back and passes none.

        Two details of the pairing matter. Nothing is retired when ``prompt`` itself raised, since
        nothing was put up (``HumanGate.approve`` raises there for a call whose turn left the screen).
        And a ``retire`` that fails is logged rather than raised: it runs in a ``finally``, where an
        exception would replace the answer this call is returning, and a channel that could not be
        told is not a reason to lose a decision the user already made.
        """
        async with self._lock:
            previous_default = self._default
            if default is not _UNSET:
                self._default = default
            self._future = asyncio.get_running_loop().create_future()
            self._context = context
            self._parse = parse
            prompted = False
            try:
                await prompt()
                prompted = True
                return await self._future
            finally:
                self._default = previous_default
                self._future = None
                self._context = None
                self._parse = None
                if prompted and retire is not None:
                    try:
                        await retire()
                    except Exception:
                        logger.warning("Could not retire an answered prompt", exc_info=True)

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


class _SwitchedAway(Exception):
    """The viewed conversation moved on while an approval waited its turn for the single slot.

    Flow control, not a failure: ``approve`` catches it and denies the call the way it does for a
    switch it noticed earlier. It travels as an exception because the prompt callable is the only
    code ``PendingRequest.ask`` runs while holding the lock that orders the askers, and that is where
    the question has to be asked one last time.
    """


class HumanGate:
    """The assistant's two human-decision points, and how a channel reply routes to them."""

    def __init__(
        self,
        ui,
        *,
        active_id: Callable[[], str],
        is_proactive: Callable[[], bool],
        turn_conversation: Callable[[], Optional[str]],
        toolset_of: Callable[[Optional[str], str], Optional[str]] = lambda agent, tool: None,
    ):
        self._ui = ui
        self._active_id = active_id
        self._is_proactive = is_proactive
        self._turn_conversation = turn_conversation
        # Names the toolset a gated call came from, so a prompt asks about `toolset.tool`: the name the
        # `confirm` or `review` entry that gated it was written in.
        self._toolset_of = toolset_of
        # Every agent's resolved approval policy, assigned once every agent has been wired
        # (core.approval.resolve_approval_policies, called from Assistant.start). None until then,
        # deliberately, rather than an empty set: this gate is built in the composition root's __init__
        # because wiring needs `approve` to hand to each agent, while the vocabulary a config entry
        # resolves against does not exist until that wiring finishes. An empty default would make the
        # window between the two read as "nothing is gated", which is the exact silent failure the
        # startup check exists to prevent, so `approve` raises in it instead. The window is wider than
        # it looks: boot's remote half runs in `start`, so it spans every MCP handshake.
        self.policies: Optional[ApprovalPolicies] = None
        # The gate [security.auto_approval] resolves to, or None when it is off, assigned from the same
        # place and at the same point as `policies` above (core.auto_approval.resolve_auto_approval,
        # called from Assistant.start). Unlike that one, None is a real answer here rather than a
        # not-yet-wired sentinel: the feature ships off, and `policies is None` already refuses every
        # call made before wiring finishes, so one sentinel covers both.
        self.auto_approval: Optional[AutoApproval] = None
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

    async def approve(self, agent: str, name: str, arguments: dict) -> bool:
        """Tool-approval gate run before each tool call (published to the model client per run).

        ``agent`` is the name the calling agent was wired with (``Assistant._gate_for`` binds it), so the
        call is judged by that agent's own policy. Ungated tools pass, and what is gated is that
        policy's ``gated`` set: the tool names startup resolved its ``confirm`` and ``review`` entries
        to, not the config entries themselves, which name a toolset each.
        A proactive/scheduled turn always auto-denies a gated tool: it is
        unattended, so nobody is watching to confirm, and a firing that fell back to the viewed
        conversation would otherwise look foreground (its turn conversation equals the viewed one) and
        wrongly prompt.
        Otherwise a reactive turn is approved only if its conversation is the one the user is
        currently viewing; a turn backgrounded by a switch auto-denies. Otherwise prompt over the
        channel and await the answer, which the serve loop routes here.

        **That one question is asked at all three points where the answer can have gone stale**, and
        each window is wider than the last: on arrival, after the review's model call, and once the
        approval slot is finally held. The third exists because the slot is single and lock-guarded,
        so a second gated call from the same concurrently dispatched round waits out the user's answer
        to the first, and nothing bounds how long that is.

        A gated tool in the agent's ``review`` list is reviewed (when ``[security.auto_approval]`` is on) just before that prompt, and
        only there: both auto-denials above run first, so a review happens exactly where a human would
        otherwise have been asked and nowhere else, and the switched-away one is asked again after the
        review, since a review is a model call the user can switch conversations during. The ordering
        is one of two independent guarantees of that, since a reactive turn is also the only path that
        opens a review context, so a call in an unattended turn reaching here would fail closed inside
        ``review_call`` anyway. A review can
        only remove the prompt, never the capability: anything it withholds, and every way it can
        fail, arrives at the same prompt this method would have shown, with one exception: a switch
        away during the review itself denies rather than prompts, since nobody is left watching the
        turn a prompt would appear on. That denial is narrower than a prompt, not wider, and it is the
        same answer the check above already gives for a switch that happened earlier. Both outcomes
        are reported as a card, because an auto-approval nobody saw is a decision made on the user's
        behalf in silence.
        """
        if self.policies is None:
            raise RuntimeError(
                "the tool-approval gate was asked about a call before startup resolved "
                f"the approval policies, so it cannot say whether {name!r} is gated. Nothing may run "
                "a tool before Assistant.start has finished wiring."
            )
        policy = self.policies.for_agent(agent)
        if name not in policy.gated:
            return True
        if self._is_proactive():
            return False
        turn_conversation = self._turn_conversation()
        if turn_conversation != self._active_id():
            return await self._deny_switched_away(name, turn_conversation)
        if self.auto_approval is not None and name in policy.review:
            # No `try`: `review_call` turns every failure into an outcome that escalates, so the fall
            # through below is the only thing a failed review can reach.
            outcome = await review_call(self.auto_approval, tool=name, arguments=arguments)
            # Asked again, because a review takes a model call (up to `timeout_seconds`, 10 by default)
            # and the check above is that old by the time it returns. A user who switched conversations
            # inside that window is owed the same answer they would have got without the feature: the
            # reviewer may approve, and the tool would then run on a turn that is no longer on screen,
            # which is the one thing that check exists to prevent. The denial comes before the card
            # rather than after it on purpose: an "auto-approved" card beside a call that was denied
            # would be a false record, and the alert below reports what actually happened. The review
            # itself is not lost either way, since `review_call` already logged what the reviewer
            # answered, whether or not this re-check goes on to deny the call.
            if self._turn_conversation() != self._active_id():
                return await self._deny_switched_away(name, turn_conversation)
            await self._ui.show_auto_approval(
                self._label(agent, name),
                arguments,
                approved=outcome.approved,
                reason=outcome.reason,
                model=outcome.model,
                conversation_id=turn_conversation,
                agent=agent,
            )
            if outcome.approved:
                return True

        async def prompt() -> None:
            # Asked a third time, here rather than above, because this runs under the lock that orders
            # the askers and the checks above are as old as the wait for it. Concurrent tool dispatch
            # ([assistant].concurrent_tools) puts a second gated call from the same round behind this
            # slot, where it waits out the user's answer to the first: an unbounded window in practice,
            # far wider than the review's. A switch during it abandons only the request holding the
            # slot, so without this the one behind would go on to ask, and ask the wrong reader: an
            # `approval` frame is never muted, so the page draws it into whatever transcript is on
            # screen, and an Allow there runs the tool on a turn nobody is watching. The conversation
            # riding on that frame is the page's own backstop for the sliver of the window even this
            # check cannot close; the check is what keeps the question from being asked at all.
            if self._turn_conversation() != self._active_id():
                raise _SwitchedAway
            await self._ui.ask_approval(
                self._label(agent, name), arguments, agent=agent, conversation_id=turn_conversation
            )

        try:
            return await self.approval.ask(prompt, retire=self._ui.retire_prompt)
        except _SwitchedAway:
            return await self._deny_switched_away(name, turn_conversation)

    def _label(self, agent: str, name: str) -> str:
        """``name`` as the prompt shows it: ``toolset.tool``, or bare when no toolset is known."""
        toolset = self._toolset_of(agent, name)
        return f"{toolset}.{name}" if toolset else name

    async def _deny_switched_away(self, name: str, turn_conversation: Optional[str]) -> bool:
        """Deny a gated call whose turn is not the conversation on screen, and say so. Always False.

        Raised rather than denied in silence: the tool result recording the refusal lands on a
        transcript the user is not reading, and the turn carries on without the tool, so nothing tells
        them their own turn lost a capability by their switching away. Not raised for a proactive
        firing, which reports itself when it ends and would otherwise raise one of these on every
        firing.

        One method, three callers, because all three are the same event at different times: the switch
        may have happened before the call reached the gate, during the review the gate ran, or while
        the call waited behind another call's prompt for the approval slot.
        """
        await self._ui.alert(
            f"A turn you switched away from asked to run {name}. It was denied automatically, "
            "because approving a tool call means reading it, and that turn is not on screen.",
            conversation_id=turn_conversation,
            # One card per tool per conversation: a loop retrying the same call must not stack a
            # card per attempt, while a second, different tool is genuinely something else to know.
            group=f"{turn_conversation}:{name}",
        )
        return False

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

        The retirement is the core's, though, and it is the same one an approval gets: what the page
        has to be told is that the question is closed, which is true of every prompt this class puts
        up and says nothing about whose vocabulary asked it.
        """
        return await self.decision.ask(
            prompt, context=context, parse=parse, default=default, retire=self._ui.retire_prompt
        )

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
