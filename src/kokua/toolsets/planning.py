"""The ``planning`` toolset: it contributes no tools, only the deep-planning workflow.

A capability with a workflow and no tools is why ``Toolset.workflow`` exists. Declaring ``"planning"``
in an agent's ``tools`` is what gives the user the ``/plan`` command, so a turn strategy is granted the
same way a tool is and no flag can disagree with the declaration.

The prompts and the reviewer vocabulary live with the workflow rather than here, unlike Kokua's
tool-bearing toolsets, which own their model-facing strings: this toolset exposes no tool surface, so
there is no schema or docstring for a reader to find here.
"""

from __future__ import annotations

from kokua.toolsets.registry import Toolset
from kokua.workflows import Workflow
from kokua.workflows.planning import PlanningWorkflow

PLANNING_WORKFLOW = Workflow(
    name="planning",
    description="Draft an explicit plan before acting, with optional agent and human review.",
    command="plan",
    usage="/plan <task>",
    build=PlanningWorkflow,
)

TOOLSET = Toolset(
    name="planning",
    description="Deep planning: plan a request before acting, with optional review of plan and result.",
    build=lambda ctx: [],
    workflow=PLANNING_WORKFLOW,
    # Planning is how the agent manages its own work rather than a domain capability, so a lean
    # supervisor declaring only it still reads as lean to the delegation guidance.
    cross_cutting=True,
)
