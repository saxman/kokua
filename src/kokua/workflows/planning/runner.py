"""The deep-planning pipeline: draft a plan, optionally review it, execute it, optionally review that.

``PlanningWorkflow`` is the runner the ``planning`` toolset's workflow builds, so ``/plan`` exists only
for an agent whose ``tools`` declares that toolset. It owns its channel sends and its transcript
rewriting and hands back a ``WorkflowResult`` the caller persists. The strings it sends to a model live
in ``prompts.py``; its two reviewers live in ``critics.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aimu.aio.channels.base import ChannelMessage

from kokua.workflows import WorkflowContext, WorkflowResult, critics
from kokua.workflows.planning import critics as review
from kokua.workflows.planning.prompts import (
    EXECUTE_PROMPT,
    PLAN_PROMPT,
    REPLAN_FEEDBACK,
    RESULT_REVISE_PROMPT,
    bullets,
)


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


@dataclass(frozen=True)
class Presentation:
    """How a planned turn shows its work. There are exactly two instances, below.

    The pipeline is the same either way -- plan, review, execute, review -- so these four flags are
    the entire difference between them, rather than two parallel implementations.

    ``stream_intermediate``
        Show each LLM call's text as it is written. When false the text is withheld and the caller
        presents it once it is final, which is what lets result_review hide an unvetted answer.
    ``announce_phases``
        Emit labeled phase headers and accumulate a trace for reload replay.
    ``show_plan_card``
        Present the plan as its own bubble. Unnecessary when it was already streamed live.
    ``reviewer_cards``
        Show each reviewer as a status card that updates with its verdict, rather than streaming its
        prose reasoning.
    """

    stream_intermediate: bool
    announce_phases: bool
    show_plan_card: bool
    reviewer_cards: bool


#: Show the outcome of each step. The default.
SUMMARY = Presentation(stream_intermediate=False, announce_phases=False, show_plan_card=True, reviewer_cards=True)
#: Show the work as it happens (`show_reasoning`), on a channel that can render phases.
VERBOSE = Presentation(stream_intermediate=True, announce_phases=True, show_plan_card=False, reviewer_cards=False)


class PlanningWorkflow:
    """Deep planning as a rich-tier workflow: plan, optionally review, execute, optionally review.

    Constructed fresh per turn, so ``_trace`` needs no reset. Rich tier because every one of its four
    presentation needs is outside what an ``AsyncRunner`` can express: labeled phases, reviewer cards, a
    human approve/edit/reject pause, and rewriting the transcript so the saved conversation reads as the
    user's own words rather than planner scaffolding.
    """

    def __init__(self, ctx: WorkflowContext):
        self._ctx = ctx
        self._agent = ctx.agent
        self._ui = ctx.ui
        self._config = ctx.config
        # The raw trace of the in-flight verbose turn: a list of {label, detail, text} phase segments,
        # accumulated by _send_phase / _run_and_capture / _stream_review. None outside a verbose turn.
        self._trace: Optional[list[dict]] = None

    async def run_turn(self) -> WorkflowResult:
        """Plan, optionally adversarially review + human review, then execute (optionally with
        adversarial result review). Returns a WorkflowResult for the caller to persist."""
        msg = self._ctx.msg
        mode = VERBOSE if self._config.show_reasoning and self._ui.supports_phases else SUMMARY
        self._trace = [] if mode.announce_phases else None
        events: list[dict] = []
        try:
            await self._phase(mode, "Planner", "drafting a plan")
            plan = await self._make_plan(msg, mode)
            critique: Optional[list[str]] = None
            if self._config.plan_review_agent:
                plan, critique = await self._plan_review_rounds(msg, plan, mode, events)
            if mode.show_plan_card:  # in verbose the plan was already streamed as it was written
                await self._send_plan(plan, critique)

            approved = plan
            if self._config.plan_review:
                approved = await self._review_with_human(plan, critique)
                if approved is None:
                    await self._ui.send("(plan rejected)", reply_to=msg)
                    return WorkflowResult(committed=False)

            if not self._config.result_review and not mode.announce_phases:
                return await self._execute_streaming(msg, approved, events)
            return await self._execute_with_review(msg, approved, mode, events)
        finally:
            self._trace = None

    async def run(self, task, generate_kwargs=None, stream=False, images=None):
        """The base-tier entry point, unused by the core (which calls ``run_turn``) and present so this
        workflow is a complete ``AsyncRunner``: that is what makes ``as_tool()`` available if planning
        is ever offered to the model as a tool."""
        raise NotImplementedError(
            "deep planning drives its own turn; the assistant calls run_turn(). Offering it to the "
            "model as a tool needs a nested variant that does not rewrite the caller's transcript."
        )

    @property
    def messages(self):
        return {"planning": list(self._agent.model_client.messages)}

    async def _review_with_human(self, plan: str, critique: Optional[list[str]]) -> Optional[str]:
        """Await the user's decision on a plan: approve (the plan), edit (their text), or reject (None).

        The reply vocabulary is here rather than in the core because it is this workflow's: "approve"
        means "the plan I was shown", which only this workflow knows how to supply.
        """

        def parse(raw: str, text: str) -> Optional[str]:
            """approve -> the plan under review, reject -> None, else the user's edited plan."""
            if text in ("approve", "yes", "y"):
                return plan
            if text in ("reject", "no", "n"):
                return None
            if text.startswith("edit:"):
                return raw.split(":", 1)[1].strip() or plan
            return raw.strip()

        return await self._ctx.decide(
            lambda: self._ui.ask_plan_review(plan, critique), parse, default=None, context=plan
        )

    async def _execute_streaming(self, msg: ChannelMessage, plan: str, events: list[dict]) -> WorkflowResult:
        """The unreviewed summary path: stream the executor straight into the reply.

        Kept separate from ``_execute_with_review`` because it is genuinely different execution, not a
        different presentation of the same one: the answer streams as the reply itself (no separate
        send), and the executor's tool calls stay in the transcript rather than being replaced by a
        clean user/assistant pair. Only the executor's prompt is rewritten, back to the user's own
        words, so the saved conversation reads naturally. The verbose path cannot use this: it needs
        the text back to record in the trace, and it must not emit a terminating frame mid-turn.
        """
        base_len = len(self._agent.model_client.messages)
        try:
            stream = await self._agent.run(
                EXECUTE_PROMPT.format(request=msg.text, plan=plan), stream=True, images=msg.images
            )
            await self._ui.send(stream, reply_to=msg)
        finally:
            # Also on the cancelled and failed paths: the executor appends its user message before it
            # can call any tool, so a turn that reported a spawn has one here to anchor the spawn's
            # cards to, and it needs the same rewrite a completed turn gets. Synchronous, so a second
            # cancellation cannot cut it short.
            self._ctx.commit_user_message(base_len, msg.text)
        return WorkflowResult(committed=True, user_index=self._ctx.user_index, subagent_events=events)

    async def _execute_with_review(
        self, msg: ChannelMessage, plan: str, mode: "Presentation", events: list[dict]
    ) -> WorkflowResult:
        """Execute, optionally review and revise, then commit one clean turn and surface the answer.

        Only the final answer reaches the transcript: the executor's scratch and every revision round
        are rolled back to ``base`` and replaced by a single user/assistant pair, so a rejected draft
        never becomes conversation history the model will later see.
        """
        base = list(self._agent.model_client.messages)
        rounds = self._config.review_rounds
        answer = ""
        try:
            await self._phase(mode, "Executor", "carrying out the plan")
            answer = await self._run_and_capture(
                EXECUTE_PROMPT.format(request=msg.text, plan=plan), msg.images, show_answer=mode.stream_intermediate
            )
            if self._config.result_review:
                for attempt in range(rounds + 1):
                    evidence = _tool_evidence(self._agent.model_client.messages[len(base) :])
                    verdict = await self._review_once(
                        mode,
                        events,
                        role="Result reviewer",
                        slug="result-review",
                        attempt=attempt,
                        card=lambda: review.review_result(self._config.model, msg.text, plan, answer, evidence),
                        stream=lambda: review.stream_result_review(
                            self._config.model, msg.text, plan, answer, evidence
                        ),
                    )
                    if verdict.approved:
                        break
                    if attempt == rounds:
                        # Out of rounds. In verbose the user watched every round, so the concerns are
                        # already on screen; in summary this appendix is the only trace of them.
                        if not mode.stream_intermediate:
                            answer += "\n\n---\n_Automated review flagged unresolved issues:_\n" + bullets(
                                verdict.issues
                            )
                        break
                    await self._phase(mode, "Executor", "revising the answer")
                    self._agent.model_client.messages = list(base)  # revise from a clean base
                    answer = await self._run_and_capture(
                        RESULT_REVISE_PROMPT.format(
                            request=msg.text, plan=plan, answer=answer, issues=bullets(verdict.issues)
                        ),
                        msg.images,
                        show_answer=mode.stream_intermediate,
                    )
        finally:
            # Runs on the cancelled and failed paths too, which is where the published index earns its
            # keep: an answer from an earlier round still commits, and its spawn cards need anchoring.
            # The pair replaces everything the executor and its revisions appended, so the user message
            # lands back at the pre-execution length however many rounds ran. Resolving from the
            # committed list is what makes "nothing to show" self-evidently unanchored: with no pair
            # there is no user message at or after len(base), so the index stays -1 rather than
            # pointing at where the *next* turn will commit. The pair already holds the user's own
            # words, so committing it here is the publish, with the rewrite a no-op.
            pair = [{"role": "user", "content": msg.text}, {"role": "assistant", "content": answer}]
            self._agent.model_client.messages = base + (pair if answer else [])
            self._ctx.commit_user_message(len(base), msg.text)

        if mode.stream_intermediate:
            await self._ui.finish_stream()  # already shown live; just terminate the streamed bubble
        else:
            await self._ui.send(answer, reply_to=msg)  # withheld until vetted; show it now
        return WorkflowResult(
            committed=True, user_index=self._ctx.user_index, subagent_events=events, trace=self._trace or []
        )

    async def _plan_review_rounds(
        self, msg: ChannelMessage, plan: str, mode: "Presentation", events: list[dict]
    ) -> tuple[str, Optional[list[str]]]:
        """Have an independent, context-free agent critique the plan; re-plan on rejection up to
        review_rounds. Returns the final plan and any residual issues (None if the reviewer approved)."""
        rounds = self._config.review_rounds
        for attempt in range(rounds + 1):
            verdict = await self._review_once(
                mode,
                events,
                role="Plan reviewer",
                slug="plan-review",
                attempt=attempt,
                card=lambda: review.review_plan(self._config.model, msg.text, plan),
                stream=lambda: review.stream_plan_review(self._config.model, msg.text, plan),
            )
            if verdict.approved:
                return plan, None
            if attempt == rounds:  # out of rounds; carry the unresolved issues forward
                return plan, verdict.issues
            await self._phase(mode, "Planner", "revising the plan")
            plan = await self._make_plan(msg, mode, feedback=verdict.issues)
        return plan, None  # unreachable (rounds >= 0)

    async def _review_once(
        self,
        mode: "Presentation",
        events: list[dict],
        *,
        role: str,
        slug: str,
        attempt: int,
        card,
        stream,
    ) -> critics.Verdict:
        """One review round, shown either as a summary card or as streamed prose.

        ``card`` and ``stream`` are thunks so only the reviewer actually used is ever started; the two
        open different AIMU calls (a plain review vs. a streamed one).
        """
        await self._phase(mode, role, f"round {attempt + 1}")
        if mode.reviewer_cards:
            verdict = await self._run_review(f"{slug}-{attempt}", role, attempt, card())
            events.append(self._verdict_event(role, attempt, verdict))
            return verdict
        return await self._stream_review(stream())

    async def _stream_review(self, open_coro) -> critics.Verdict:
        """Stream a reviewer's prose reasoning live (captured into the current phase segment for replay),
        then finalize and return its verdict. Emits no summary card -- the prose is the output."""
        client, stream = await open_coro
        # A channel without live streaming still drains the stream (so the reviewer call completes)
        # and yields "", which is right here: the prose is display-only, the verdict is what matters.
        text = await self._ui.stream_activity(stream, show_answer=True)
        if self._trace:  # attach the reviewer's prose to the current phase segment
            self._trace[-1]["text"] = text
        return await critics.finalize_verdict(client)

    async def _make_plan(self, msg: ChannelMessage, mode: "Presentation", feedback: Optional[list[str]] = None) -> str:
        """Run the agent to produce a plan, keeping the planning exchange out of the saved conversation.

        Tools stay enabled so the planner can web-search and consult its skill catalog; the turns it adds
        (planner prompt, tool calls, plan) are rolled back afterwards -- planning is scratch work, and the
        approved plan is re-supplied to the executor. ``feedback`` (reviewer issues) drives a re-plan round.
        """
        prompt = PLAN_PROMPT.format(request=msg.text)
        if feedback:
            prompt += REPLAN_FEEDBACK.format(issues=bullets(feedback))
        base = list(self._agent.model_client.messages)
        try:
            plan = await self._run_and_capture(prompt, msg.images, show_answer=mode.stream_intermediate)
        finally:
            self._agent.model_client.messages = base
        return plan

    async def _run_and_capture(self, prompt: str, images, *, show_answer: bool = False) -> str:
        """Run the agent, showing its agentic loop (thinking/tool calls) live, and return the final text.

        By default the final text is withheld (the caller shows it once it's ready). With
        ``show_answer=True`` (verbose trace) the text is streamed live too. Channels without
        ``stream_activity`` (e.g. the CLI) fall back to a plain non-streaming run.
        """
        if self._ui.supports_streamed_activity:
            stream = await self._agent.run(prompt, stream=True, images=images)
            text = await self._ui.stream_activity(stream, show_answer=show_answer)
        else:  # the caller needs this text, so run non-streaming rather than draining to ""
            result = await self._agent.run(prompt, images=images)
            text = result if isinstance(result, str) else str(result)
        if self._trace:  # verbose trace: attach this call's output to the current phase segment
            self._trace[-1]["text"] = text
        return text

    async def _phase(self, mode: "Presentation", label: str, detail: str = "") -> None:
        """Open a labeled phase, in a mode that announces them.

        Also opens a new segment in the in-flight trace, so the streamed output that follows is
        captured under this phase for reload replay. A no-op in summary mode even on a phase-capable
        channel: summary shows outcomes, not the steps that produced them.
        """
        if not mode.announce_phases:
            return
        if self._trace is not None:
            self._trace.append({"label": label, "detail": detail, "text": ""})
        await self._ui.show_phase(label, detail)

    async def _run_review(self, sid: str, role: str, round_: int, coro) -> critics.Verdict:
        """Show a running sub-agent card, await the reviewer, then update the card with its verdict."""
        await self._ui.show_subagent({"id": sid, "role": role, "status": "running", "round": round_})
        verdict = await coro
        status = "approved" if verdict.approved else "rejected"
        await self._ui.show_subagent(
            {"id": sid, "role": role, "status": status, "issues": list(verdict.issues), "round": round_}
        )
        return verdict

    @staticmethod
    def _verdict_event(role: str, round_: int, verdict: critics.Verdict) -> dict:
        """The persisted (id-less) form of a reviewer verdict, for replay."""
        status = "approved" if verdict.approved else "rejected"
        return {"role": role, "status": status, "issues": list(verdict.issues), "round": round_}

    async def _send_plan(self, plan_text: str, critique: Optional[list[str]] = None) -> None:
        """Show the plan (with any residual reviewer concerns), as a plan frame if the channel supports it."""
        text = plan_text
        if critique:
            text += "\n\n---\n**Reviewer's remaining concerns:**\n" + bullets(critique)
        await self._ui.show_plan(text)
