"""How the core drives each tier: a base-tier runner streams into the reply, a rich one owns the turn."""

from __future__ import annotations

from pathlib import Path

from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamChunk, StreamingContentType

from kokua.config import AssistantConfig
from kokua.config.table import RuntimeSetting
from kokua.core.assistant import Assistant
from kokua.toolsets.planning import PLANNING_WORKFLOW
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
    # _BaseRunner is self-contained (never touches ctx.agent), which is what makes a base-tier turn
    # unpersisted: nothing anchors it, so _persist's snapshot carries no messages for this turn.
    assert assistant._store.get(assistant._active_id).messages == []


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


async def test_a_workflow_reads_its_own_declared_settings(tmp_path):
    """``ctx.settings`` is the carrying toolset's own ``config.toml`` section, keyed by the toolset name
    the workflow shares. Named "planning" here because that is the section this config sets, not because
    the core knows anything about planning."""
    channel = FakeChannel()
    captured = {}

    class _Peek:
        def __init__(self, ctx):
            self.ctx = ctx
            captured["rounds"] = ctx.settings.review_rounds
            captured["review"] = ctx.settings.plan_review

        async def run(self, task, generate_kwargs=None, stream=False, images=None):
            return "ok"

        @property
        def messages(self):
            return {}

        async def run_turn(self):
            await self.ctx.ui.send("ok")
            return WorkflowResult(committed=False)

    config = _config(tmp_path, toolset_settings={"planning": {"review_rounds": 7, "plan_review": True}})
    assistant = await Assistant.create(config, channel, client=MockAsyncModelClient(["unused"]))
    peeking = Workflow(name="planning", description="P.", command="plan", usage=PLANNING_WORKFLOW.usage, build=_Peek)

    await assistant._handle(
        ChannelMessage(text="x", channel="fake"), conversation_id=assistant._active_id, workflow=peeking
    )

    assert captured == {"rounds": 7, "review": True}


async def test_a_workflow_reading_an_undeclared_setting_fails_loudly(tmp_path):
    """A view rather than a dict lookup returning None: a workflow asking for a key its toolset never
    declared has a bug in the declaration, and a silent None would surface as the setting's default."""
    seen = {}

    class _Peek:
        def __init__(self, ctx):
            try:
                ctx.settings.never_declared
            except AttributeError as error:
                seen["error"] = str(error)

        async def run(self, task, generate_kwargs=None, stream=False, images=None):
            return "ok"

        @property
        def messages(self):
            return {}

        async def run_turn(self):
            return WorkflowResult(committed=False)

    config = _config(tmp_path, toolset_settings={"planning": {"plan_review": True}})
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["unused"]))
    peeking = Workflow(name="planning", description="P.", command="plan", usage="/plan <t>", build=_Peek)

    await assistant._handle(
        ChannelMessage(text="x", channel="fake"), conversation_id=assistant._active_id, workflow=peeking
    )

    assert "never_declared" in seen["error"] and "settings" in seen["error"]


async def test_a_hot_contributed_setting_written_mid_turn_reaches_the_same_context(tmp_path):
    """``SettingsView`` wraps ``config.toolset_settings[<name>]`` in place rather than copying it, so a
    write landing while a turn is already running -- the same mutation ``SettingsApplier.apply`` performs
    per hot setting, via ``RuntimeSetting.write`` -- is visible on the next read of ``ctx.settings``, with
    no need to rebuild the context. That aliasing is currently the only thing making a live settings
    change reach an in-flight turn, and nothing else here would notice a refactor that snapshotted the
    bucket into the view instead."""
    captured = {}

    class _Peek:
        def __init__(self, ctx):
            self.ctx = ctx

        async def run(self, task, generate_kwargs=None, stream=False, images=None):
            return "ok"

        @property
        def messages(self):
            return {}

        async def run_turn(self):
            captured["before"] = self.ctx.settings.review_rounds
            # Applied directly with RuntimeSetting.write, not through SettingsApplier.apply: that path
            # takes an exclusive gate hold, which this turn already holds, and would deadlock against it.
            RuntimeSetting("review_rounds", "planning", int, toolset="planning").write(
                self.ctx.config, 9, lambda field, value: None
            )
            captured["after"] = self.ctx.settings.review_rounds
            return WorkflowResult(committed=False)

    config = _config(tmp_path, toolset_settings={"planning": {"review_rounds": 2}})
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["unused"]))
    peeking = Workflow(name="planning", description="P.", command="plan", usage="/plan <t>", build=_Peek)

    await assistant._handle(
        ChannelMessage(text="x", channel="fake"), conversation_id=assistant._active_id, workflow=peeking
    )

    assert captured == {"before": 2, "after": 9}
