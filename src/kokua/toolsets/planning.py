"""The ``planning`` toolset: it contributes no tools, only the deep-planning workflow and its settings.

A capability with a workflow and no tools is why ``Toolset.workflow`` exists. Declaring ``"planning"``
in an agent's ``tools`` is what gives the user the ``/plan`` command, so a turn strategy is granted the
same way a tool is and no flag can disagree with the declaration.

The ``[planning]`` config section is declared here too, which is what makes deep planning configurable
the same way a third party's workflow would be: nothing about these five keys is known to
``AssistantConfig``, the settings panel, or the persist path beyond the declaration below.

The prompts and the reviewer vocabulary live with the workflow rather than here, unlike Kokua's
tool-bearing toolsets, which own their model-facing strings: this toolset exposes no tool surface, so
there is no schema or docstring for a reader to find here.
"""

from __future__ import annotations

from kokua.toolsets.registry import Setting, Toolset
from kokua.workflows import Workflow
from kokua.workflows.planning import PlanningWorkflow

PLANNING_WORKFLOW = Workflow(
    name="planning",
    description="Draft an explicit plan before acting, with optional agent and human review.",
    command="plan",
    usage="/plan <task>",
    build=PlanningWorkflow,
)

#: The ``[planning]`` section of config.toml, owned here rather than by ``AssistantConfig``: these keys
#: exist because this toolset declares them, and the workflow reads them through ``ctx.settings``. The
#: four flags are hot (the settings panel and ``update_config`` change them mid-session);
#: ``review_rounds`` is not, because a round budget is read once per turn and there is no live surface
#: that offers it.
PLANNING_SETTINGS: tuple[Setting, ...] = (
    Setting("plan_review", bool, False, hot=True),
    Setting("plan_review_agent", bool, False, hot=True),
    Setting("result_review", bool, False, hot=True),
    Setting("show_reasoning", bool, False, hot=True),
    Setting("review_rounds", int, 2),
)

TOOLSET = Toolset(
    name="planning",
    description="Deep planning: plan a request before acting, with optional review of plan and result.",
    build=lambda ctx: [],
    workflow=PLANNING_WORKFLOW,
    settings=PLANNING_SETTINGS,
    # Planning is how the agent manages its own work rather than a domain capability, so a lean
    # supervisor declaring only it still reads as lean to the delegation guidance.
    cross_cutting=True,
)
