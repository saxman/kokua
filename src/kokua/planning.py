"""Deep-planning / review orchestration, extracted from the assistant core.

PlanRunner runs the opt-in `/plan` flow: draft a plan, optionally have an independent reviewer critique
it and a human approve it, then execute (optionally with an independent result review). It holds the
agent/channel/config and an injected human plan-review callback, does its own channel sends and transcript
commits, and returns a PlanResult the caller persists. Constructed fresh per planned turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from aimu import aio
from aimu.aio.channels.base import ChannelMessage

from . import review
from .config import AssistantConfig

PLAN_PROMPT = """\
Before doing any work, produce an explicit plan for how you will accomplish the request below. Do NOT \
carry out the work or produce the final deliverable yet -- only plan.

Request:
{request}

Your plan should:
- State the goal and what a complete, correct answer looks like.
- Give the concrete steps you will take, in order, as a numbered markdown list.
- For each step, name the specific tool, skill, or MCP service you will use. Where a needed capability \
is missing, say so and how you will get it: build a new skill (author_skill), connect an MCP service \
(add_mcp_server), and web-search to find a suitable MCP service or documentation when that helps.
- Note what you will verify before finishing.

You may use read-only tools (e.g. web search) to inform the plan, but make no changes yet. Respond with \
just the plan."""

EXECUTE_PROMPT = """\
Carry out the following approved plan to fully answer the original request. Follow the plan, adapting if \
you discover something that requires it, and use the tools/skills it names.

Original request:
{request}

Approved plan:
{plan}"""

# Feedback blocks fed back into a replan / revise round after an adversarial reviewer rejects.
REPLAN_FEEDBACK = "\n\nAn independent reviewer rejected your previous plan for these reasons:\n{issues}\n\nProduce a new plan that addresses them."

RESULT_REVISE_PROMPT = """\
Your previous answer was checked by an independent reviewer and rejected. Revise it to fully address the \
issues, returning the complete corrected answer (not just the changes).

Original request:
{request}

Approved plan:
{plan}

Your previous answer:
{answer}

Reviewer's issues:
{issues}"""


def _bullets(issues: list[str]) -> str:
    """Render reviewer issues as a markdown bullet list (or a dash if empty)."""
    return "\n".join(f"- {i}" for i in issues) or "- (no specific issues given)"


def _tool_evidence(messages: list[dict], max_chars: int = 2000) -> str:
    """Render the tool results in ``messages`` (an executor transcript slice) as a compact evidence block
    for the result reviewer, so it judges against what the agent actually retrieved rather than its own
    (possibly stale) memory. Each tool result is truncated to ``max_chars``. Returns "" if no tools ran."""
    names: dict = {}  # tool_call_id -> tool name, to label results that lack a "name" of their own
    lines: list[str] = []
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            names[call.get("id")] = call.get("function", {}).get("name")
        if msg.get("role") == "tool":
            name = msg.get("name") or names.get(msg.get("tool_call_id")) or "tool"
            content = str(msg.get("content", ""))
            if len(content) > max_chars:
                content = content[:max_chars] + " ...[truncated]"
            lines.append(f"- {name}: {content}")
    return "\n".join(lines)


@dataclass
class PlanResult:
    """Outcome of a planned turn, for the caller to persist. ``committed`` is False on plan-rejection
    (no committed turn to anchor replay cards to); otherwise ``user_index`` is the index of the committed
    user message and ``subagent_events`` / ``trace`` are the reload-replay metadata for that turn."""

    committed: bool
    user_index: int = -1
    subagent_events: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)


class PlanRunner:
    """Runs one deep-planning turn. Constructed fresh per turn, so ``_trace`` needs no reset."""

    def __init__(
        self,
        agent: aio.SkillAgent,
        channel,
        config: AssistantConfig,
        on_plan_review: Callable[[str, Optional[list[str]]], Awaitable[Optional[str]]],
    ):
        self._agent = agent
        self._channel = channel
        self._config = config
        self._on_plan_review = on_plan_review
        # The raw trace of the in-flight verbose turn: a list of {label, detail, text} phase segments,
        # accumulated by _send_phase / _run_and_capture / _stream_review. None outside a verbose turn.
        self._trace: Optional[list[dict]] = None

    async def run(self, msg: ChannelMessage) -> PlanResult:
        """Deep planning: plan, optionally adversarially review + human review, then execute (optionally
        with adversarial result review). Returns a PlanResult for the caller to persist."""
        if self._config.show_reasoning and getattr(self._channel, "send_phase", None) is not None:
            return await self._verbose_planned_turn(msg)  # verbose trace needs a phase-capable channel
        events: list[dict] = []
        plan_text = await self._make_plan(msg)
        critique: Optional[list[str]] = None
        if self._config.plan_review_agent:
            plan_text, critique = await self._adversarial_plan_review(msg, plan_text, events)
        await self._send_plan(plan_text, critique)
        approved = plan_text
        if self._config.plan_review:
            approved = await self._on_plan_review(plan_text, critique)
            if approved is None:
                await self._channel.send("(plan rejected)", reply_to=msg)
                return PlanResult(committed=False)
        if self._config.result_review:
            answer = await self._execute_reviewed(msg, approved, events)
            user_index = len(self._agent.model_client.messages) - 2  # [..., user, assistant]
            await self._channel.send(answer, reply_to=msg)
            return PlanResult(committed=True, user_index=user_index, subagent_events=events)
        base_len = len(self._agent.model_client.messages)
        stream = await self._agent.run(
            EXECUTE_PROMPT.format(request=msg.text, plan=approved), stream=True, images=msg.images
        )
        await self._channel.send(stream, reply_to=msg)
        msgs = self._agent.model_client.messages
        if len(msgs) > base_len and msgs[base_len].get("role") == "user":
            msgs[base_len]["content"] = msg.text
        return PlanResult(committed=True, user_index=base_len, subagent_events=events)

    async def _verbose_planned_turn(self, msg: ChannelMessage) -> PlanResult:
        """Deep planning with the full trace visible: every LLM call streams under a labeled phase and
        every plan/result version is shown. The whole raw trace is captured (self._trace) and returned for
        reload replay. Only the final answer is committed; this overrides result_review's gate."""
        self._trace = []
        try:
            await self._send_phase("Planner", "drafting a plan")
            plan = await self._make_plan(msg, show_answer=True)
            critique: Optional[list[str]] = None
            if self._config.plan_review_agent:
                plan, critique = await self._verbose_plan_review(msg, plan)
            approved = plan
            if self._config.plan_review:
                approved = await self._on_plan_review(plan, critique)
                if approved is None:
                    await self._channel.send("(plan rejected)", reply_to=msg)
                    return PlanResult(committed=False)
            await self._verbose_execute(msg, approved)  # streams + commits the final answer
            user_index = len(self._agent.model_client.messages) - 2  # [..., user, asst]
            trace = self._trace
            await self._send_done()
            return PlanResult(committed=True, user_index=user_index, trace=trace)
        finally:
            self._trace = None

    async def _verbose_plan_review(self, msg: ChannelMessage, plan: str) -> tuple[str, Optional[list[str]]]:
        """Stream each plan-review round's prose reasoning; re-plan visibly on rejection. No summary
        card -- the streamed reasoning (and a following 'revising' phase on rejection) is the output."""
        rounds = self._config.review_rounds
        for attempt in range(rounds + 1):
            await self._send_phase("Plan reviewer", f"round {attempt + 1}")
            verdict = await self._stream_review(review.stream_plan_review(self._config.model, msg.text, plan))
            if verdict.approved:
                return plan, None
            if attempt == rounds:
                return plan, verdict.issues
            await self._send_phase("Planner", "revising the plan")
            plan = await self._make_plan(msg, feedback=verdict.issues, show_answer=True)
        return plan, None

    async def _verbose_execute(self, msg: ChannelMessage, plan: str) -> str:
        """Stream the executor and each result-review round visibly; every version is shown. Commits only
        the final answer to a clean transcript."""
        base = list(self._agent.model_client.messages)
        rounds = self._config.review_rounds
        answer = ""
        try:
            await self._send_phase("Executor", "carrying out the plan")
            answer = await self._run_and_capture(
                EXECUTE_PROMPT.format(request=msg.text, plan=plan), msg.images, show_answer=True
            )
            if self._config.result_review:
                for attempt in range(rounds + 1):
                    await self._send_phase("Result reviewer", f"round {attempt + 1}")
                    evidence = _tool_evidence(self._agent.model_client.messages[len(base) :])
                    verdict = await self._stream_review(
                        review.stream_result_review(self._config.model, msg.text, plan, answer, evidence),
                    )
                    if verdict.approved or attempt == rounds:
                        break
                    await self._send_phase("Executor", "revising the answer")
                    self._agent.model_client.messages = list(base)  # revise from a clean base
                    answer = await self._run_and_capture(
                        RESULT_REVISE_PROMPT.format(
                            request=msg.text, plan=plan, answer=answer, issues=_bullets(verdict.issues)
                        ),
                        msg.images,
                        show_answer=True,
                    )
        finally:
            pair = [{"role": "user", "content": msg.text}, {"role": "assistant", "content": answer}]
            self._agent.model_client.messages = base + (pair if answer else [])
        return answer

    async def _stream_review(self, open_coro) -> "review.Verdict":
        """Stream a reviewer's prose reasoning live (captured into the current phase segment for replay),
        then finalize and return its verdict. Emits no summary card -- the prose is the output."""
        client, stream = await open_coro
        stream_activity = getattr(self._channel, "stream_activity", None)
        if stream_activity is not None:
            text = await stream_activity(stream, show_answer=True)
        else:  # no streaming channel: drain so the reviewer call completes
            text = ""
            async for _ in stream:
                pass
        if self._trace:  # attach the reviewer's prose to the current phase segment
            self._trace[-1]["text"] = text
        return await review.finalize_verdict(client)

    async def _send_done(self) -> None:
        """End a verbose turn: finalize the last streamed bubble and clear the processing state."""
        send = getattr(self._channel, "send_done", None)
        if send is not None:
            await send()

    async def _send_subagent(self, event: dict) -> None:
        """Show a sub-agent activity card if the channel supports it (web); other channels ignore it."""
        send = getattr(self._channel, "send_subagent", None)
        if send is not None:
            await send(event)

    async def _make_plan(
        self, msg: ChannelMessage, feedback: Optional[list[str]] = None, *, show_answer: bool = False
    ) -> str:
        """Run the agent to produce a plan, keeping the planning exchange out of the saved conversation.

        Tools stay enabled so the planner can web-search and consult its skill catalog; the turns it adds
        (planner prompt, tool calls, plan) are rolled back afterwards -- planning is scratch work, and the
        approved plan is re-supplied to the executor in ``run``. ``feedback`` (reviewer issues) drives
        a re-plan round; ``show_answer`` streams the plan text live (verbose trace).
        """
        prompt = PLAN_PROMPT.format(request=msg.text)
        if feedback:
            prompt += REPLAN_FEEDBACK.format(issues=_bullets(feedback))
        base = list(self._agent.model_client.messages)
        try:
            plan = await self._run_and_capture(prompt, msg.images, show_answer=show_answer)
        finally:
            self._agent.model_client.messages = base
        return plan

    async def _run_and_capture(self, prompt: str, images, *, show_answer: bool = False) -> str:
        """Run the agent, showing its agentic loop (thinking/tool calls) live, and return the final text.

        By default the final text is withheld (the caller shows it once it's ready). With
        ``show_answer=True`` (verbose trace) the text is streamed live too. Channels without
        ``stream_activity`` (e.g. the CLI) fall back to a plain non-streaming run.
        """
        stream_activity = getattr(self._channel, "stream_activity", None)
        if stream_activity is None:
            result = await self._agent.run(prompt, images=images)
            text = result if isinstance(result, str) else str(result)
        else:
            stream = await self._agent.run(prompt, stream=True, images=images)
            text = await stream_activity(stream, show_answer=show_answer)
        if self._trace:  # verbose trace: attach this call's output to the current phase segment
            self._trace[-1]["text"] = text
        return text

    async def _send_phase(self, label: str, detail: str = "") -> None:
        """Announce a labeled phase (verbose trace) if the channel supports it; others ignore it.

        Also opens a new segment in the in-flight trace (self._trace) so the streamed output that
        follows is captured under this phase for reload replay.
        """
        if self._trace is not None:
            self._trace.append({"label": label, "detail": detail, "text": ""})
        send = getattr(self._channel, "send_phase", None)
        if send is not None:
            await send(label, detail)

    async def _run_review(self, sid: str, role: str, round_: int, coro) -> "review.Verdict":
        """Show a running sub-agent card, await the reviewer, then update the card with its verdict."""
        await self._send_subagent({"id": sid, "role": role, "status": "running", "round": round_})
        verdict = await coro
        status = "approved" if verdict.approved else "rejected"
        await self._send_subagent(
            {"id": sid, "role": role, "status": status, "issues": list(verdict.issues), "round": round_}
        )
        return verdict

    @staticmethod
    def _verdict_event(role: str, round_: int, verdict: "review.Verdict") -> dict:
        """The persisted (id-less) form of a reviewer verdict, for replay."""
        status = "approved" if verdict.approved else "rejected"
        return {"role": role, "status": status, "issues": list(verdict.issues), "round": round_}

    async def _adversarial_plan_review(
        self, msg: ChannelMessage, plan: str, events: list[dict]
    ) -> tuple[str, Optional[list[str]]]:
        """Have an independent, context-free agent critique the plan; re-plan on rejection up to
        review_rounds. Emits reviewer cards, appends verdicts to ``events``, and returns the final plan and
        any residual issues (None if the reviewer approved)."""
        rounds = self._config.review_rounds
        for attempt in range(rounds + 1):
            verdict = await self._run_review(
                f"plan-review-{attempt}",
                "Plan reviewer",
                attempt,
                review.review_plan(self._config.model, msg.text, plan),
            )
            events.append(self._verdict_event("Plan reviewer", attempt, verdict))
            if verdict.approved:
                return plan, None
            if attempt == rounds:  # out of rounds; carry the unresolved issues forward
                return plan, verdict.issues
            plan = await self._make_plan(msg, feedback=verdict.issues)
        return plan, None  # unreachable (rounds >= 0)

    async def _execute_reviewed(self, msg: ChannelMessage, plan: str, events: list[dict]) -> str:
        """Execute non-streaming, have an independent agent review the answer, revise on rejection up to
        review_rounds, then commit a single clean turn (user's words + final answer) and return it. Emits
        reviewer cards and appends verdicts to ``events``."""
        base = list(self._agent.model_client.messages)
        rounds = self._config.review_rounds
        answer = ""
        try:
            answer = await self._run_and_capture(EXECUTE_PROMPT.format(request=msg.text, plan=plan), msg.images)
            for attempt in range(rounds + 1):
                evidence = _tool_evidence(self._agent.model_client.messages[len(base) :])
                verdict = await self._run_review(
                    f"result-review-{attempt}",
                    "Result reviewer",
                    attempt,
                    review.review_result(self._config.model, msg.text, plan, answer, evidence),
                )
                events.append(self._verdict_event("Result reviewer", attempt, verdict))
                if verdict.approved:
                    break
                if attempt == rounds:
                    answer += "\n\n---\n_Automated review flagged unresolved issues:_\n" + _bullets(verdict.issues)
                    break
                self._agent.model_client.messages = list(base)  # revise from a clean base
                answer = await self._run_and_capture(
                    RESULT_REVISE_PROMPT.format(
                        request=msg.text, plan=plan, answer=answer, issues=_bullets(verdict.issues)
                    ),
                    msg.images,
                )
        finally:
            # Commit one clean turn; the executor's scratch (and revision rounds) stay out of history.
            pair = [{"role": "user", "content": msg.text}, {"role": "assistant", "content": answer}]
            self._agent.model_client.messages = base + (pair if answer else [])
        return answer

    async def _send_plan(self, plan_text: str, critique: Optional[list[str]] = None) -> None:
        """Show the plan (with any residual reviewer concerns), as a plan frame if the channel supports it."""
        text = plan_text
        if critique:
            text += "\n\n---\n**Reviewer's remaining concerns:**\n" + _bullets(critique)
        send = getattr(self._channel, "send_plan", None)
        if send is not None:
            await send(text)
        else:
            await self._channel.send(f"Plan:\n\n{text}")
