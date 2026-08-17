"""The workflow protocol: the two tiers, and what a context publishes back to the core."""

from __future__ import annotations

from kokua.workflows import Workflow, WorkflowContext, WorkflowResult, is_rich


class _Plain:
    async def run(self, task, generate_kwargs=None, stream=False, images=None):
        return "answer"

    @property
    def messages(self):
        return {}


class _Rich(_Plain):
    async def run_turn(self) -> WorkflowResult:
        return WorkflowResult(committed=True)


def test_a_plain_runner_is_base_tier():
    assert is_rich(_Plain()) is False


def test_a_runner_with_run_turn_is_rich_tier():
    assert is_rich(_Rich()) is True


def test_a_workflow_declares_its_command_and_usage():
    workflow = Workflow(
        name="planning",
        description="Plan first.",
        command="plan",
        usage="/plan <task>",
        build=lambda ctx: _Rich(),
    )
    assert workflow.command == "plan"
    assert workflow.usage == "/plan <task>"


def test_a_fresh_context_reports_no_committed_message():
    ctx = WorkflowContext(
        agent=None,
        ui=None,
        config=None,
        settings=None,
        msg=None,
        state=None,
        decide=None,
        commit_user_message=None,
    )
    assert ctx.user_index == -1


def test_publishing_an_index_is_readable_by_the_core():
    ctx = WorkflowContext(
        agent=None,
        ui=None,
        config=None,
        settings=None,
        msg=None,
        state=None,
        decide=None,
        commit_user_message=None,
    )
    ctx.publish_user_index(4)
    assert ctx.user_index == 4
