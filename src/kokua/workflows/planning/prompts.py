"""Every string deep planning puts in front of a model, and the two helpers that only shape text.

Collected in one module so the pipeline in ``runner.py`` reads as control flow, and so what the planner,
the executor, and each reviewer are actually asked can be reviewed as prose without stepping through the
turn that sends it.
"""

from __future__ import annotations

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

# Appended to both reviewer prompts. Reviewers were rejecting correct answers as "hallucinated" because
# they trusted their own (stale) training knowledge over the agent's fresher, tool-retrieved data. Tell
# them to distrust memory and verify with tools before flagging.
_VERIFY_GUIDANCE = (
    " Important: your own built-in knowledge may be out of date, and the agent may have had access to more "
    "current information than you do. A claim that merely disagrees with what you remember is NOT by itself "
    "evidence of fabrication. Before flagging anything as inaccurate, fabricated, or hallucinated, verify it "
    "with your tools (web search, fetch a page, check the current date/time) and prefer freshly retrieved "
    "information over your own recollection. If you cannot verify a claim either way, do not reject on "
    "suspicion -- note it as unverified in your suggestions instead."
)

# Appended only to the result reviewer, which is additionally shown the evidence the agent gathered.
_EVIDENCE_GUIDANCE = (
    " You may also be shown an Evidence section with the tool results the agent used to produce its answer. "
    "Treat those retrieved sources as more current than your own memory, and still spot-check them with "
    "your own tools where it matters."
)

PLAN_REVIEW_SYSTEM = (
    """\
You are an independent reviewer with NO access to the conversation. You are given a user request and a \
plan another agent produced to fulfill it. Judge only whether the plan is sound: complete enough to fully \
satisfy the request, correct in its approach, sensible in the tools/skills/services it chooses, and \
including any needed verification. Be adversarial but fair -- flag concrete defects (missing steps, wrong \
or missing tools, unjustified assumptions, no verification), not style or things you simply cannot see \
from the request alone. Set approved=true only if the plan is ready to execute as-is."""
    + _VERIFY_GUIDANCE
)

RESULT_REVIEW_SYSTEM = (
    """\
You are an independent reviewer with NO access to the conversation. You are given a user request, the plan \
that was followed, and the final result another agent produced. Judge only whether the result fully and \
correctly satisfies the request and the plan: is it complete, accurate (no likely fabrication), and does \
it meet what the plan set out to verify? Be adversarial but fair -- flag concrete problems, not style. \
Set approved=true only if the result is ready to send to the user."""
    + _VERIFY_GUIDANCE
    + _EVIDENCE_GUIDANCE
)

PLAN_INPUT = "Request:\n{request}\n\nPlan:\n{plan}"
_RESULT_INPUT = "Request:\n{request}\n\nPlan:\n{plan}\n\nFinal result:\n{answer}"
_EVIDENCE_BLOCK = "\n\nEvidence the agent gathered (tool results it used to produce the answer):\n{evidence}"


def result_input(request: str, plan: str, answer: str, evidence: str) -> str:
    """The result reviewer's user message: request/plan/answer, plus the agent's evidence when present."""
    user_input = _RESULT_INPUT.format(request=request, plan=plan, answer=answer)
    if evidence:
        user_input += _EVIDENCE_BLOCK.format(evidence=evidence)
    return user_input


def bullets(issues: list[str]) -> str:
    """Render reviewer issues as a markdown bullet list (or a dash if empty)."""
    return "\n".join(f"- {i}" for i in issues) or "- (no specific issues given)"
