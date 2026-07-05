"""Mock-only tests for deep planning mode (plan -> optional review -> execute)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from helpers import MockAsyncModelClient
from kokua.assistant import PLAN_PROMPT, Assistant
from kokua.config import AssistantConfig

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
    base = {"data_dir": tmp_path, "memory": False}
    base.update(overrides)
    return AssistantConfig(**base)


async def test_autonomous_planned_turn_plans_then_executes(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["THE PLAN", "THE ANSWER"])  # plan phase, then execute phase
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(ChannelMessage(text="do the thing", channel="fake"), plan=True)

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

    await assistant._handle(ChannelMessage(text="hi", channel="fake"))  # plan defaults off

    assert channel.sent == ["JUST THE ANSWER"]  # no plan surfaced, single run


async def _resolve_when_pending(assistant, value, *, approve=False):
    """Set the pending-plan future once the reviewed plan is awaiting a decision.

    ``approve=True`` resolves with the current plan text (what the serve loop does for "approve");
    otherwise resolves with ``value`` (an edited plan, or None to reject).
    """
    for _ in range(1000):
        if assistant._pending_plan is not None and not assistant._pending_plan.done():
            assistant._pending_plan.set_result(assistant._pending_plan_text if approve else value)
            return
        await asyncio.sleep(0)
    raise AssertionError("plan review never became pending")


async def test_review_approve_executes(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN", "ANSWER"])
    assistant = await Assistant.create(_config(tmp_path, plan_review=True), channel, client=client)

    turn = asyncio.create_task(assistant._handle(ChannelMessage(text="do X", channel="fake"), plan=True))
    await _resolve_when_pending(assistant, None, approve=True)
    await turn

    assert "ANSWER" in channel.sent


async def test_review_reject_skips_execution(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN"])  # only the plan; execution must not run (would need a 2nd)
    assistant = await Assistant.create(_config(tmp_path, plan_review=True), channel, client=client)

    turn = asyncio.create_task(assistant._handle(ChannelMessage(text="do X", channel="fake"), plan=True))
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
    assistant = await Assistant.create(_config(tmp_path, plan_review=True), channel, client=client)

    turn = asyncio.create_task(assistant._handle(ChannelMessage(text="do X", channel="fake"), plan=True))
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
    assert s["plan_review"] is False
    assert "plan_mode" not in s  # the global toggle is gone; planning is per-request

    await assistant.apply_settings({"plan_review": True, "generate_kwargs": {}})
    assert assistant._config.plan_review is True
    assert assistant.current_settings()["plan_review"] is True
