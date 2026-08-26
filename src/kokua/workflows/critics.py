"""An independent, context-free critic any workflow can reuse to check its own work.

The critic runs as a *fresh* agent holding only the system prompt it is given, so it sees none of the
calling conversation -- an independent judge, not the author defending its own work. It is a
*tool-using* critic: it runs a bounded tool-calling loop over a curated verification toolset
(`REVIEWER_TOOLS`: web lookup, arithmetic, and the current date/time) so it can check recency and
factual/numeric claims instead of rejecting anything it cannot verify from its input alone. The typed
verdict is then extracted in a second, tool-less structured call (`finalize_verdict`); that call stays
`use_tools=False` because a forced schema and forced tools conflict on Anthropic. The reviewer toolset
deliberately excludes the user's memory/documents, skills, and MCP mutation -- see `REVIEWER_TOOLS`.

What counts as approvable is the caller's, never this module's: the prompts are arguments, so a
workflow brings its own standard (see :mod:`kokua.workflows.planning.critics` for the pair deep
planning uses). These functions are module-level so a workflow can orchestrate bounded
reject-and-retry loops around them, and so tests can monkeypatch them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from aimu import aio
from aimu.tools import builtin

from kokua.core.metrics import record_event

# The reviewer's verification toolset: an independent critic that can look things up and check
# arithmetic, but has no access to user state. web = get_weather/get_webpage/web_search/wikipedia;
# `calculate` for numeric claims; plus the current date/time (the original motivation: reviewers were
# rejecting correct recency claims for date-unawareness). Deliberately EXCLUDES the memory/document
# stores, skill authoring, and MCP add/remove.
#
# `calculate` rather than the whole `builtin.compute` group, because that group's other member is
# `execute_python`, which runs arbitrary code with the user's privileges and no sandbox. A reviewer
# cannot be approval-gated -- an autonomous critic has nobody to ask mid-review, and a gate it could
# not satisfy would just deadlock it -- so mounting `execute_python` here would hand it the one
# capability `[security] confirm_tools` exists to hold back, with the gate structurally unable to
# apply. Arithmetic is the only thing a verdict actually needs computed, and `calculate` covers it.
REVIEWER_TOOLS: list[Callable] = [*builtin.web, builtin.calculate, builtin.get_current_date_and_time]

_VERDICT_PROMPT = (
    "Based on your assessment above, report your verdict: whether it is approved, the concrete issues "
    "(if any), and any suggestions."
)


@dataclass
class Verdict:
    """An independent reviewer's structured judgement of whatever it was asked to assess."""

    approved: bool
    issues: list[str] = field(default_factory=list)
    suggestions: str = ""


def reviewer_agent(
    model: Optional[str],
    system: str,
    tools: Optional[list[Callable]] = None,
    thinking: Optional[Union[bool, str]] = None,
    generate_kwargs: Optional[dict] = None,
) -> aio.Agent:
    """A fresh, context-free reviewer agent with the verification toolset (an independent, tool-using
    critic). ``tools`` overrides ``REVIEWER_TOOLS``, for a workflow that needs its judge grounded in a
    different (or narrower) toolset than the default. No ``tool_approval`` gate: the reviewer runs
    unattended, so the toolset is curated to hold nothing that would need one -- see ``REVIEWER_TOOLS``.
    Public so a workflow that needs a judge shaped differently (a debate round, a scored rubric) can
    build on the same independent agent instead of assembling its own.

    ``thinking`` is the reviewer's reasoning effort and ``generate_kwargs`` its generation parameters,
    and both are the caller's to decide: a reviewer is not an ``[agents.*]`` agent, so
    ``[assistant].thinking`` and ``[assistant.generation]`` are the only tiers it has, exactly as
    ``[assistant].model`` is for the model above it. ``thinking`` is an agent field, so it applies to
    every turn of the review, including the typed verdict.

    Wired to :func:`kokua.core.metrics.record_event` unconditionally, not a parameter: a critic builds
    its own client, so a caller's own scoped sink never reaches it, and a review's model turns are part
    of what the turn that asked for it cost. ``record_event`` is a module-level constant that reads the
    running turn off a contextvar when an event actually fires, so a reviewer built here reports into
    whichever turn is running at review time with no plumbing back to the caller.
    """
    client = aio.client(model, system=system)
    # Only when there is something to set: this is the tier above the model card's own tuned profile, so
    # an empty write would shadow it. The caller's to decide, like the model and the effort -- a reviewer
    # is no [agents.*] agent, so [assistant.generation] is the only tier it has.
    if generate_kwargs:
        client.default_generate_kwargs = dict(generate_kwargs)
    return aio.Agent(
        client,
        tools=REVIEWER_TOOLS if tools is None else tools,
        max_iterations=6,  # bound verification cost
        final_answer_prompt=_VERDICT_PROMPT,  # force an assessment if it hits the cap mid-tool-call
        thinking=thinking,
        events=record_event,
    )


async def review(
    model: Optional[str],
    system: str,
    user_input: str,
    thinking: Optional[Union[bool, str]] = None,
    generate_kwargs: Optional[dict] = None,
) -> Verdict:
    """Run one context-free review: a bounded tool-calling assessment, then the typed verdict.

    The prompts are the caller's, not this module's: a critic is the reusable half (a fresh agent, a
    curated verification toolset, a typed verdict), while what counts as approvable is the workflow's
    own business.
    """
    agent = reviewer_agent(model, system, thinking=thinking, generate_kwargs=generate_kwargs)
    await agent.run(user_input)  # free-text tool-calling loop; assessment lands in the agent's client
    return await finalize_verdict(agent.model_client)


async def stream_review(
    model: Optional[str],
    system: str,
    user_input: str,
    thinking: Optional[Union[bool, str]] = None,
    generate_kwargs: Optional[dict] = None,
):
    """Open a streamed review. Returns ``(client, chunk_stream)``; the caller streams the chunks (the
    reviewer's prose reasoning and tool activity) then calls :func:`finalize_verdict`.

    Streamed rather than structured because a workflow that shows its work needs the reviewer's
    *reasoning* visible, and a structured (``schema=``) call cannot stream readable prose (on Anthropic
    it is a forced tool: JSON only, no thinking). So the assessment loop streams and the typed verdict
    is extracted from that reasoning afterwards, on the same client.
    """
    agent = reviewer_agent(model, system, thinking=thinking, generate_kwargs=generate_kwargs)
    stream = await agent.run(user_input, stream=True)
    return agent.model_client, stream


async def finalize_verdict(client) -> Verdict:
    """Extract the structured verdict from the reviewer's assessment (now in ``client``'s context)."""
    return await client.chat(_VERDICT_PROMPT, schema=Verdict, use_tools=False)
