"""Mock-only tests for deep planning mode (plan -> optional review -> execute)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.helpers import MockAsyncModelClient
from kokua.core.assistant import Assistant
from kokua.core.steering import ENTRY_STEERING_SOURCE
from kokua.toolsets.planning import PLANNING_WORKFLOW
from kokua.workflows.planning import PlanningWorkflow
from kokua.workflows.planning.prompts import PLAN_PROMPT
from kokua.config import AssistantConfig
from tests.channels import example_agents, planning_settings

from aimu import aio
from aimu.aio.channels.base import Channel, ChannelMessage
from aimu.models import StreamingContentType


class FakeChannel(Channel):
    name = "fake"

    def __init__(self):
        self.sent: list[str] = []

    async def receive(self):
        if False:
            yield None

    async def send(self, content, *, reply_to=None) -> None:
        if isinstance(content, str):
            self.sent.append(content)
            return
        parts = []
        async for chunk in content:
            if chunk.phase == StreamingContentType.GENERATING:
                parts.append(chunk.content)
        self.sent.append("".join(parts))


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {
        "data_dir": tmp_path,
        "agents": example_agents(),
        "entry_agent": "assistant",
        "toolset_settings": planning_settings(),
    }
    base.update(overrides)
    return AssistantConfig(**base)


def test_the_planning_workflow_inherits_the_runner_base_that_carries_as_tool():
    """``as_tool()`` is a concrete method ``AsyncRunner`` provides, so implementing ``run`` and
    ``messages`` without inheriting would leave planning unable to reach the model as a tool -- while
    still passing every other test here, since Kokua's own driver only probes for ``run_turn``."""
    assert issubclass(PlanningWorkflow, aio.AsyncRunner)
    assert hasattr(PlanningWorkflow, "as_tool")


async def test_autonomous_planned_turn_plans_then_executes(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["THE PLAN", "THE ANSWER"])  # plan phase, then execute phase
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="do the thing", channel="fake"),
        conversation_id=assistant._active_id,
        workflow=PLANNING_WORKFLOW,
    )

    # The plan was surfaced first (no send_plan on this channel -> plain-text fallback), then the answer.
    assert any("THE PLAN" in s for s in channel.sent)
    assert "THE ANSWER" in channel.sent
    assert channel.sent.index(next(s for s in channel.sent if "THE PLAN" in s)) < channel.sent.index("THE ANSWER")

    # The saved conversation is clean: the user's own words, no planner scaffolding, plan kept out.
    messages = assistant._agent.model_client.messages
    assert any(m.get("role") == "user" and m.get("content") == "do the thing" for m in messages)
    assert not any(PLAN_PROMPT[:30] in str(m.get("content", "")) for m in messages)


async def test_unplanned_turn_is_a_single_turn(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["JUST THE ANSWER"])  # only one response -> only one run happens
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="hi", channel="fake"), conversation_id=assistant._active_id
    )  # plan defaults off
    assert channel.sent == ["JUST THE ANSWER"]  # no plan surfaced, single run


async def _resolve_when_pending(assistant, value, *, approve=False):
    """Set the pending-plan future once the reviewed plan is awaiting a decision.

    ``approve=True`` resolves with the current plan text (what the serve loop does for "approve");
    otherwise resolves with ``value`` (an edited plan, or None to reject).
    """
    for _ in range(1000):
        pending = assistant._human.decision
        if pending.pending:
            pending.resolve(pending.context if approve else value)
            return
        await asyncio.sleep(0)
    raise AssertionError("plan review never became pending")


async def test_review_approve_executes(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN", "ANSWER"])
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(plan_review=True)), channel, client=client
    )

    turn = asyncio.create_task(
        assistant._handle(
            ChannelMessage(text="do X", channel="fake"),
            conversation_id=assistant._active_id,
            workflow=PLANNING_WORKFLOW,
        )
    )
    await _resolve_when_pending(assistant, None, approve=True)
    await turn

    assert "ANSWER" in channel.sent


async def test_review_reject_skips_execution(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN"])  # only the plan; execution must not run (would need a 2nd)
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(plan_review=True)), channel, client=client
    )

    turn = asyncio.create_task(
        assistant._handle(
            ChannelMessage(text="do X", channel="fake"),
            conversation_id=assistant._active_id,
            workflow=PLANNING_WORKFLOW,
        )
    )
    await _resolve_when_pending(assistant, None)  # reject
    await turn

    assert any("rejected" in s for s in channel.sent)
    assert client._call_count == 1  # only the plan run happened


async def test_review_edit_executes_edited_plan(tmp_path):
    channel = FakeChannel()

    class RecordingMock(MockAsyncModelClient):
        prompts: list = []

        async def _chat(self, user_message, *a, **k):
            RecordingMock.prompts.append(user_message)  # captured before the post-run rewrite scrubs it
            return await super()._chat(user_message, *a, **k)

    RecordingMock.prompts = []
    client = RecordingMock(["PLAN", "ANSWER"])
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(plan_review=True)), channel, client=client
    )

    turn = asyncio.create_task(
        assistant._handle(
            ChannelMessage(text="do X", channel="fake"),
            conversation_id=assistant._active_id,
            workflow=PLANNING_WORKFLOW,
        )
    )
    await _resolve_when_pending(assistant, "MY EDITED PLAN")
    await turn

    assert "ANSWER" in channel.sent
    # The executor was driven by the edited plan (the execute prompt embeds it).
    assert any("MY EDITED PLAN" in p for p in RecordingMock.prompts)


async def test_current_settings_and_apply_carry_plan_flags(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    s = assistant.current_settings()
    # Namespaced, because the key belongs to the planning toolset rather than to the core: the panel is
    # one flat object, and two toolsets may both reasonably want a "plan_review".
    assert s["planning.plan_review"] is False
    assert "plan_review" not in s  # the un-namespaced key is nobody's
    assert "plan_mode" not in s  # the global toggle is gone; planning is per-request

    await assistant.apply_settings({"planning.plan_review": True, "generate_kwargs": {}})
    assert assistant._config.toolset_settings["planning"]["plan_review"] is True
    assert assistant.current_settings()["planning.plan_review"] is True


class _ActivityChannel(FakeChannel):
    """A channel that can render an agent's loop live, which is the branch ``_run_and_capture`` takes
    when one is available. The plain ``FakeChannel`` above takes the other."""

    async def stream_activity(self, chunks, *, show_answer=False) -> str:
        parts = []
        async for chunk in chunks:
            if chunk.phase == StreamingContentType.GENERATING and isinstance(chunk.content, str):
                parts.append(chunk.content)
        return "".join(parts)


@pytest.mark.parametrize("channel_type", [FakeChannel, _ActivityChannel])
async def test_every_run_in_a_planned_turn_steers_on_the_conversations_own_cursor(tmp_path, channel_type):
    """Every model call here is the *entry* agent's, so every one opens the conversation's own cursor
    rather than a worker's independent one: ``close`` measures what to re-submit from that cursor, so a
    run opening an independent one would leave its position at zero and re-run every message the turn
    did deliver as a turn of its own.

    Parametrized over a channel that renders an agent's loop live and one that cannot, because the
    planner reaches the model through a different call on each.
    """
    channel = channel_type()
    client = MockAsyncModelClient(["THE PLAN", "THE ANSWER"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    agent = assistant._agent
    run = agent.run
    sources = []

    async def capture(prompt, **kwargs):
        sources.append(kwargs.get("steering"))
        return await run(prompt, **kwargs)

    agent.run = capture
    await assistant._handle(
        ChannelMessage(text="do the thing", channel="fake"),
        conversation_id=assistant._active_id,
        workflow=PLANNING_WORKFLOW,
    )

    assert len(sources) == 2  # the planner drafting, then the executor answering
    assert all(source is ENTRY_STEERING_SOURCE for source in sources)
