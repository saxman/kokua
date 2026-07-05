"""Independent, context-free reviewers for deep planning mode (adversarial plan + result review).

Each reviewer runs as a *fresh* agent with only a reviewer system prompt, so it sees none of the main
agent's conversation -- an independent critic, not the author defending its own work. It is a *tool-using*
critic: it runs a bounded tool-calling loop over a curated verification toolset (`REVIEWER_TOOLS`: web
lookup, computation, and the current date/time) so it can check recency and factual/numeric claims
instead of rejecting anything it cannot verify from the request alone. The typed verdict is then
extracted in a second, tool-less structured call (`finalize_verdict`); that call stays `use_tools=False`
because a forced schema and forced tools conflict on Anthropic. The reviewer toolset deliberately
excludes the user's memory/documents, skills, and MCP mutation -- see `REVIEWER_TOOLS`. These functions
are module-level so the assistant can orchestrate bounded replan/revise loops and tests can monkeypatch
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from aimu import aio
from aimu.tools import builtin

# The reviewer's verification toolset: an independent critic that can look things up and compute, but has
# no access to user state. web = get_weather/get_webpage/web_search/wikipedia; compute = calculate/
# execute_python (so the reviewer can run calculations to check numeric claims); plus the current date/
# time (the original motivation: reviewers were rejecting correct recency claims for date-unawareness).
# Deliberately EXCLUDES memory/document stores, skill authoring, and MCP add/remove.
#
# NOTE (known limitation, tracked in README): execute_python runs arbitrary code with full machine
# access and, unlike the main agent, the reviewer has no approval gate -- an autonomous critic must not
# block on the user mid-review. This is an intentional short-term tradeoff for calculation support.
REVIEWER_TOOLS: list[Callable] = [*builtin.web, *builtin.compute, builtin.get_current_date_and_time]

PLAN_REVIEW_SYSTEM = """\
You are an independent reviewer with NO access to the conversation. You are given a user request and a \
plan another agent produced to fulfill it. Judge only whether the plan is sound: complete enough to fully \
satisfy the request, correct in its approach, sensible in the tools/skills/services it chooses, and \
including any needed verification. Be adversarial but fair -- flag concrete defects (missing steps, wrong \
or missing tools, unjustified assumptions, no verification), not style or things you simply cannot see \
from the request alone. Set approved=true only if the plan is ready to execute as-is."""

RESULT_REVIEW_SYSTEM = """\
You are an independent reviewer with NO access to the conversation. You are given a user request, the plan \
that was followed, and the final result another agent produced. Judge only whether the result fully and \
correctly satisfies the request and the plan: is it complete, accurate (no likely fabrication), and does \
it meet what the plan set out to verify? Be adversarial but fair -- flag concrete problems, not style. \
Set approved=true only if the result is ready to send to the user."""

_PLAN_INPUT = "Request:\n{request}\n\nPlan:\n{plan}"
_RESULT_INPUT = "Request:\n{request}\n\nPlan:\n{plan}\n\nFinal result:\n{answer}"


@dataclass
class Verdict:
    """An independent reviewer's structured judgement of a plan or a result."""

    approved: bool
    issues: list[str] = field(default_factory=list)
    suggestions: str = ""


def _reviewer_agent(model: Optional[str], system: str, tools: Optional[list[Callable]] = None) -> aio.Agent:
    """A fresh, context-free reviewer agent with the verification toolset (an independent, tool-using
    critic). ``tools`` overrides ``REVIEWER_TOOLS`` (tests pass their own). No ``tool_approval`` gate:
    the toolset is curated and the reviewer must run unattended. Factored out so tests can monkeypatch it."""
    return aio.Agent(
        aio.client(model, system=system),
        tools=REVIEWER_TOOLS if tools is None else tools,
        max_iterations=6,  # bound verification cost
        final_answer_prompt=_VERDICT_PROMPT,  # force an assessment if it hits the cap mid-tool-call
    )


async def _review(model: Optional[str], system: str, user_input: str) -> Verdict:
    """Run one context-free review: a bounded tool-calling assessment, then extract the typed verdict."""
    agent = _reviewer_agent(model, system)
    await agent.run(user_input)  # free-text tool-calling loop; assessment lands in the agent's client
    return await finalize_verdict(agent.model_client)


async def review_plan(model: Optional[str], request: str, plan: str) -> Verdict:
    """Independently review a plan against the request (no conversation context)."""
    return await _review(model, PLAN_REVIEW_SYSTEM, _PLAN_INPUT.format(request=request, plan=plan))


async def review_result(model: Optional[str], request: str, plan: str, answer: str) -> Verdict:
    """Independently review a final result against the request and plan (no conversation context)."""
    return await _review(model, RESULT_REVIEW_SYSTEM, _RESULT_INPUT.format(request=request, plan=plan, answer=answer))


# --- Streamed reviewers (verbose trace) ------------------------------------------------------
# For the verbose trace we want the reviewer's reasoning *visible*. A structured (schema=) call can't
# stream readable prose (on Anthropic it's a forced tool: JSON only, no thinking), so we stream the
# reviewer's tool-calling assessment loop -- which streams thinking, prose, and tool activity -- and then
# extract the typed verdict from that reasoning on the same client.

_VERDICT_PROMPT = (
    "Based on your assessment above, report your verdict: whether it is approved, the concrete issues "
    "(if any), and any suggestions."
)


async def stream_plan_review(model: Optional[str], request: str, plan: str):
    """Open a streamed plan review. Returns ``(client, chunk_stream)``; the caller streams the chunks
    (the reviewer's prose reasoning and tool activity) then calls :func:`finalize_verdict` for the typed
    verdict."""
    agent = _reviewer_agent(model, PLAN_REVIEW_SYSTEM)
    stream = await agent.run(_PLAN_INPUT.format(request=request, plan=plan), stream=True)
    return agent.model_client, stream


async def stream_result_review(model: Optional[str], request: str, plan: str, answer: str):
    """Open a streamed result review (see :func:`stream_plan_review`)."""
    agent = _reviewer_agent(model, RESULT_REVIEW_SYSTEM)
    stream = await agent.run(_RESULT_INPUT.format(request=request, plan=plan, answer=answer), stream=True)
    return agent.model_client, stream


async def finalize_verdict(client) -> Verdict:
    """Extract the structured verdict from the reviewer's assessment (now in ``client``'s context)."""
    return await client.chat(_VERDICT_PROMPT, schema=Verdict, use_tools=False)
