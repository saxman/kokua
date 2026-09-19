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

from kokua.config.schema import ResolvedReviewer
from kokua.workflows import critics
from kokua.workflows.planning.prompts import PLAN_INPUT, PLAN_REVIEW_SYSTEM, RESULT_REVIEW_SYSTEM, result_input


async def review_plan(
    reviewer: ResolvedReviewer,
    request: str,
    plan: str,
    name: str = "Plan reviewer",
) -> critics.Verdict:
    """Independently review a plan against the request (no conversation context).

    ``reviewer`` carries the model, effort, sampling, and standard, resolved from
    ``[reviewers.plan]`` over the ``[assistant]`` tiers. An undeclared table leaves
    ``system_message`` empty, and the shipped standard below is what applies: planning's own prompt
    is the default a config overrides, not a value config has to restate to keep.

    ``name`` defaults to the same label ``workflows/planning/runner.py`` shows for this reviewer in the
    UI, so a turn's cost export groups a plan review's calls under the label a reader already recognizes
    rather than a second, unrelated name for the same round.
    """
    return await critics.review(
        reviewer.model,
        reviewer.system_message or PLAN_REVIEW_SYSTEM,
        PLAN_INPUT.format(request=request, plan=plan),
        thinking=reviewer.thinking,
        generate_kwargs=reviewer.generation,
        name=name,
    )


async def review_result(
    reviewer: ResolvedReviewer,
    request: str,
    plan: str,
    answer: str,
    evidence: str = "",
    name: str = "Result reviewer",
) -> critics.Verdict:
    """Independently review a final result against the request and plan (no conversation context).

    ``reviewer`` carries the model, effort, sampling, and standard, resolved from
    ``[reviewers.result]`` over the ``[assistant]`` tiers; see :func:`review_plan`.

    ``evidence`` is the agent's tool-result transcript (see ``runner._tool_evidence``); when given, the
    reviewer weighs it as fresher than its own memory instead of rejecting on stale-knowledge suspicion.
    ``name`` defaults to the same label the UI shows for this reviewer; see :func:`review_plan`."""
    return await critics.review(
        reviewer.model,
        reviewer.system_message or RESULT_REVIEW_SYSTEM,
        result_input(request, plan, answer, evidence),
        thinking=reviewer.thinking,
        generate_kwargs=reviewer.generation,
        name=name,
    )


async def stream_plan_review(
    reviewer: ResolvedReviewer,
    request: str,
    plan: str,
    name: str = "Plan reviewer",
):
    """Open a streamed plan review (see :func:`kokua.workflows.critics.stream_review`). Returns
    ``(client, chunk_stream)``; the caller streams the chunks, then finalizes the verdict. ``reviewer``
    and ``name`` are as in :func:`review_plan`."""
    return await critics.stream_review(
        reviewer.model,
        reviewer.system_message or PLAN_REVIEW_SYSTEM,
        PLAN_INPUT.format(request=request, plan=plan),
        thinking=reviewer.thinking,
        generate_kwargs=reviewer.generation,
        name=name,
    )


async def stream_result_review(
    reviewer: ResolvedReviewer,
    request: str,
    plan: str,
    answer: str,
    evidence: str = "",
    name: str = "Result reviewer",
):
    """Open a streamed result review (see :func:`stream_plan_review`). ``reviewer`` is as in
    :func:`review_result`. ``evidence`` is the agent's tool-result transcript, weighed as fresher than
    the reviewer's own memory when present. ``name`` defaults as in :func:`review_result`."""
    return await critics.stream_review(
        reviewer.model,
        reviewer.system_message or RESULT_REVIEW_SYSTEM,
        result_input(request, plan, answer, evidence),
        thinking=reviewer.thinking,
        generate_kwargs=reviewer.generation,
        name=name,
    )
