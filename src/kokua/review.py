"""Independent, context-free reviewers for deep planning mode (adversarial plan + result review).

Each reviewer runs on a *fresh* model client with only a reviewer system prompt, so it sees none of the
main agent's conversation -- an independent critic, not the author defending its own work. The review is
a single structured-output call (`chat(..., schema=Verdict, use_tools=False)`): `use_tools=False` keeps a
reviewer tool-less, which also sidesteps Anthropic's forced-tool/schema conflict. These are module-level
so the assistant can orchestrate bounded replan/revise loops and tests can monkeypatch them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from aimu import aio

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


async def _review(model: Optional[str], system: str, user_input: str) -> Verdict:
    """Run one context-free structured review on a fresh, tool-less client."""
    client = aio.client(model, system=system)
    return await client.chat(user_input, schema=Verdict, use_tools=False)


async def review_plan(model: Optional[str], request: str, plan: str) -> Verdict:
    """Independently review a plan against the request (no conversation context)."""
    return await _review(model, PLAN_REVIEW_SYSTEM, _PLAN_INPUT.format(request=request, plan=plan))


async def review_result(model: Optional[str], request: str, plan: str, answer: str) -> Verdict:
    """Independently review a final result against the request and plan (no conversation context)."""
    return await _review(model, RESULT_REVIEW_SYSTEM, _RESULT_INPUT.format(request=request, plan=plan, answer=answer))


# --- Streamed reviewers (verbose trace) ------------------------------------------------------
# For the verbose trace we want the reviewer's reasoning *visible*. A structured (schema=) call can't
# stream readable prose (on Anthropic it's a forced tool: JSON only, no thinking), so we run a normal
# free-text reasoning turn -- which streams thinking + prose -- and then extract the typed verdict from
# that reasoning on the same fresh client.

_VERDICT_PROMPT = (
    "Based on your assessment above, report your verdict: whether it is approved, the concrete issues "
    "(if any), and any suggestions."
)


async def stream_plan_review(model: Optional[str], request: str, plan: str):
    """Open a streamed free-text plan review. Returns ``(client, chunk_stream)``; the caller streams the
    chunks (the reviewer's prose reasoning) then calls :func:`finalize_verdict` for the typed verdict."""
    client = aio.client(model, system=PLAN_REVIEW_SYSTEM)
    stream = await client.chat(_PLAN_INPUT.format(request=request, plan=plan), stream=True, use_tools=False)
    return client, stream


async def stream_result_review(model: Optional[str], request: str, plan: str, answer: str):
    """Open a streamed free-text result review (see :func:`stream_plan_review`)."""
    client = aio.client(model, system=RESULT_REVIEW_SYSTEM)
    stream = await client.chat(
        _RESULT_INPUT.format(request=request, plan=plan, answer=answer), stream=True, use_tools=False
    )
    return client, stream


async def finalize_verdict(client) -> Verdict:
    """Extract the structured verdict from the reviewer's streamed assessment (now in ``client``'s context)."""
    return await client.chat(_VERDICT_PROMPT, schema=Verdict, use_tools=False)
