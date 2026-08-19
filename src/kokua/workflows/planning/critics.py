"""Deep planning's two reviewers: its own standards, over the shared independent critic.

The critic in :mod:`kokua.workflows.critics` is prompt-free on purpose, so what counts as an approvable
plan or an approvable answer is stated here, next to the rest of planning's prompts, rather than in the
reusable half. These four wrappers are all planning adds: the pairing of a prompt to a critic, and the
shape of the reviewer's user message.

They reach the critic through the ``critics`` module rather than by importing its functions, so a test
(or a future workflow doing the same) that replaces ``kokua.workflows.critics.reviewer_agent`` still
affects the reviews planning runs.
"""

from __future__ import annotations

from typing import Optional, Union

from kokua.workflows import critics
from kokua.workflows.planning.prompts import PLAN_INPUT, PLAN_REVIEW_SYSTEM, RESULT_REVIEW_SYSTEM, result_input


async def review_plan(
    model: Optional[str], request: str, plan: str, thinking: Optional[Union[bool, str]] = None
) -> critics.Verdict:
    """Independently review a plan against the request (no conversation context)."""
    return await critics.review(
        model, PLAN_REVIEW_SYSTEM, PLAN_INPUT.format(request=request, plan=plan), thinking=thinking
    )


async def review_result(
    model: Optional[str],
    request: str,
    plan: str,
    answer: str,
    evidence: str = "",
    thinking: Optional[Union[bool, str]] = None,
) -> critics.Verdict:
    """Independently review a final result against the request and plan (no conversation context).

    ``evidence`` is the agent's tool-result transcript (see ``runner._tool_evidence``); when given, the
    reviewer weighs it as fresher than its own memory instead of rejecting on stale-knowledge suspicion."""
    return await critics.review(
        model, RESULT_REVIEW_SYSTEM, result_input(request, plan, answer, evidence), thinking=thinking
    )


async def stream_plan_review(
    model: Optional[str], request: str, plan: str, thinking: Optional[Union[bool, str]] = None
):
    """Open a streamed plan review (see :func:`kokua.workflows.critics.stream_review`). Returns
    ``(client, chunk_stream)``; the caller streams the chunks, then finalizes the verdict."""
    return await critics.stream_review(
        model, PLAN_REVIEW_SYSTEM, PLAN_INPUT.format(request=request, plan=plan), thinking=thinking
    )


async def stream_result_review(
    model: Optional[str],
    request: str,
    plan: str,
    answer: str,
    evidence: str = "",
    thinking: Optional[Union[bool, str]] = None,
):
    """Open a streamed result review (see :func:`stream_plan_review`). ``evidence`` is the agent's
    tool-result transcript, weighed as fresher than the reviewer's own memory when present."""
    return await critics.stream_review(
        model, RESULT_REVIEW_SYSTEM, result_input(request, plan, answer, evidence), thinking=thinking
    )
