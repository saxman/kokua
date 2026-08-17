"""How the core drives each tier: a base-tier runner streams into the reply, a rich one owns the turn."""

from __future__ import annotations

from pathlib import Path

from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamChunk, StreamingContentType

from kokua.config import AssistantConfig
from kokua.core.assistant import Assistant
from kokua.workflows import Workflow, WorkflowResult
from tests.channels import FakeChannel, example_agents
from tests.helpers import MockAsyncModelClient


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "agents": example_agents(), "entry_agent": "assistant"}
    base.update(overrides)
    return AssistantConfig(**base)


class _BaseRunner:
    """A minimal AsyncRunner: no Kokua knowledge at all."""

    def __init__(self, text: str):
        self._text = text

    async def run(self, task, generate_kwargs=None, stream=False, images=None):
        if not stream:
            return f"{self._text}:{task}"

        async def chunks():
            yield StreamChunk(StreamingContentType.GENERATING, f"{self._text}:{task}", agent="t", iteration=0)

        return chunks()

    @property
    def messages(self):
        return {}


class _RichRunner:
    def __init__(self, ctx):
        self.ctx = ctx

    async def run(self, task, generate_kwargs=None, stream=False, images=None):
        raise AssertionError("the core must call run_turn on a rich runner, not run")

    @property
    def messages(self):
        return {}

    async def run_turn(self) -> WorkflowResult:
        await self.ctx.ui.send("rich reply", reply_to=self.ctx.msg)
        self.ctx.publish_user_index(0)
        return WorkflowResult(committed=True, user_index=0, trace=[{"label": "L", "detail": "d", "text": "t"}])


async def test_a_base_tier_workflow_streams_into_the_reply(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["unused"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    workflow = Workflow(
        name="echo",
        description="Echo.",
        command="echo",
        usage="/echo <text>",
        build=lambda ctx: _BaseRunner("echoed"),
    )

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake"),
        conversation_id=assistant._active_id,
        workflow=workflow,
    )

    assert channel.sent == ["echoed:hello"]


async def test_a_rich_tier_workflow_owns_its_turn_and_its_trace(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["unused"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    workflow = Workflow(
        name="rich",
        description="Rich.",
        command="rich",
        usage="/rich <text>",
        build=lambda ctx: _RichRunner(ctx),
    )

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake"),
        conversation_id=assistant._active_id,
        workflow=workflow,
    )

    assert channel.sent == ["rich reply"]
    session = assistant._book.get(assistant._active_id)
    assert session.metadata["trace"]["0"] == [{"label": "L", "detail": "d", "text": "t"}]
