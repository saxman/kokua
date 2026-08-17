"""Command dispatch: a workflow's command exists only where its toolset is declared."""

from __future__ import annotations

import asyncio

import pytest

from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamChunk, StreamingContentType

from kokua.config import AssistantConfig
from kokua.config.file import ConfigError
from kokua.core.assistant import Assistant
from kokua.toolsets.agents import build_command_map, undeclared_workflow_commands, validated_registry
from kokua.toolsets.registry import Toolset, register
from kokua.workflows import Workflow
from tests.channels import FakeChannel, _config, example_agents
from tests.helpers import MockAsyncModelClient


def _workflow(command: str) -> Workflow:
    return Workflow(name=command, description="W.", command=command, usage=f"/{command} <x>", build=lambda ctx: None)


def _registry(*toolsets: Toolset):
    return register([("test", list(toolsets))])


def test_a_declared_workflow_gets_its_command():
    agents = example_agents()
    agents["assistant"].tools = ["planning"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(Toolset(name="planning", description="P.", build=lambda ctx: [], workflow=_workflow("plan")))

    assert set(build_command_map(config, registry)) == {"plan"}


def test_the_shipped_entry_agent_earns_the_plan_command():
    """The one link between the shipped `[agents.assistant].tools` and `/plan` existing at all: drop
    "planning" from that list and the command (and the web UI's Plan toggle) silently stops working."""
    config = AssistantConfig(agents=example_agents(), entry_agent="assistant")

    commands = build_command_map(config, validated_registry(config))

    assert commands["plan"].usage == "/plan <task>"


def test_an_undeclared_workflow_gets_no_command():
    agents = example_agents()
    agents["assistant"].tools = ["time"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(
        Toolset(name="time", description="T.", build=lambda ctx: []),
        Toolset(name="planning", description="P.", build=lambda ctx: [], workflow=_workflow("plan")),
    )

    assert build_command_map(config, registry) == {}


def test_a_workflow_may_not_claim_a_reserved_command():
    agents = example_agents()
    agents["assistant"].tools = ["stopper"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(Toolset(name="stopper", description="S.", build=lambda ctx: [], workflow=_workflow("stop")))

    with pytest.raises(ConfigError, match="reserved"):
        build_command_map(config, registry)


def test_undeclared_workflow_commands_excludes_declared_and_includes_undeclared():
    agents = example_agents()
    agents["assistant"].tools = ["declared"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(
        Toolset(name="declared", description="D.", build=lambda ctx: [], workflow=_workflow("d")),
        Toolset(name="undeclared", description="U.", build=lambda ctx: [], workflow=_workflow("u")),
    )

    assert undeclared_workflow_commands(config, registry) == {"u": "undeclared"}


def test_two_workflows_may_not_claim_one_command():
    agents = example_agents()
    agents["assistant"].tools = ["a", "b"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(
        Toolset(name="a", description="A.", build=lambda ctx: [], workflow=_workflow("go")),
        Toolset(name="b", description="B.", build=lambda ctx: [], workflow=_workflow("go")),
    )

    with pytest.raises(ConfigError, match="both offer the /go command"):
        build_command_map(config, registry)


# --- command shape: dispatch only ever matches a lowercase, whitespace-free word ------------------


def test_an_empty_command_is_rejected():
    agents = example_agents()
    agents["assistant"].tools = ["planning"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(Toolset(name="planning", description="P.", build=lambda ctx: [], workflow=_workflow("")))

    with pytest.raises(ConfigError, match="not a single lowercase word"):
        build_command_map(config, registry)


def test_a_command_containing_whitespace_is_rejected():
    agents = example_agents()
    agents["assistant"].tools = ["planning"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(Toolset(name="planning", description="P.", build=lambda ctx: [], workflow=_workflow("do it")))

    with pytest.raises(ConfigError, match="not a single lowercase word"):
        build_command_map(config, registry)


def test_a_non_lowercase_command_is_rejected():
    """Dispatch lowercases the incoming word before lookup, so an uppercase command could never be
    reached -- and worse, it would slip past the reserved-word check under its own, different case
    (``"Stop"`` isn't ``"stop"``) and then be silently shadowed by the real `/stop` at runtime."""
    agents = example_agents()
    agents["assistant"].tools = ["stopper"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")
    registry = _registry(Toolset(name="stopper", description="S.", build=lambda ctx: [], workflow=_workflow("Stop")))

    with pytest.raises(ConfigError, match="not a single lowercase word"):
        build_command_map(config, registry)


# --- the serve loop's own parsing: word/task split, unknown commands, empty task ------------------


class _RecordingRunner:
    """A base-tier runner that records the task text it is handed and echoes it back."""

    def __init__(self):
        self.tasks: list[str] = []

    async def run(self, task, generate_kwargs=None, stream=False, images=None):
        self.tasks.append(task)
        if not stream:
            return f"ran:{task}"

        async def chunks():
            yield StreamChunk(StreamingContentType.GENERATING, f"ran:{task}", agent="t", iteration=0)

        return chunks()

    @property
    def messages(self):
        return {}


async def _run_one_message(assistant: Assistant, channel: FakeChannel) -> None:
    """Drive the serve loop over ``channel``'s fixed inbound list, then let whatever turn it started
    finish. ``FakeChannel.receive()`` is a finite generator (no ``/stop`` needed to end it), so a
    workflow-dispatching turn (e.g. ``/plan``) completing normally is enough to end this helper."""
    await assistant._serve_channel()
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:
        await asyncio.gather(info.handle.task, return_exceptions=True)


async def test_a_matched_command_routes_to_its_workflow_with_the_task_text(tmp_path):
    channel = FakeChannel(inbound=["/plan hello"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["unused"]))
    runner = _RecordingRunner()
    assistant._workflows = {
        "plan": Workflow(name="plan", description="P.", command="plan", usage="/plan <task>", build=lambda ctx: runner)
    }

    await _run_one_message(assistant, channel)

    assert runner.tasks == ["hello"]
    assert channel.sent == ["ran:hello"]


async def test_extra_spaces_after_the_slash_do_not_corrupt_the_task(tmp_path):
    """Regression for a length-based re-slice that assumed exactly one character preceded the word:
    it undercounted whenever more than one space followed the slash, running the workflow on a
    truncated, off-by-one task ("an hello" instead of "hello")."""
    channel = FakeChannel(inbound=["/  plan hello"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["unused"]))
    runner = _RecordingRunner()
    assistant._workflows = {
        "plan": Workflow(name="plan", description="P.", command="plan", usage="/plan <task>", build=lambda ctx: runner)
    }

    await _run_one_message(assistant, channel)

    assert runner.tasks == ["hello"]


async def test_an_unrecognized_command_runs_a_plain_turn(tmp_path):
    channel = FakeChannel(inbound=["/foo bar"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["a reply"]))
    # No toolset offers "foo" (the shipped config's only command is /plan), so the text runs through
    # unchanged rather than being read as a command.

    await _run_one_message(assistant, channel)

    assert channel.sent == ["a reply"]
    assert assistant._agent.model_client.messages[0]["content"] == "/foo bar"


async def test_a_known_command_with_no_task_replies_with_its_usage(tmp_path):
    channel = FakeChannel(inbound=["/plan"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    assistant._workflows = {
        "plan": Workflow(
            name="plan", description="P.", command="plan", usage="/plan <task>", build=lambda ctx: _RecordingRunner()
        )
    }

    await _run_one_message(assistant, channel)

    assert channel.sent == ["Usage: /plan <task>"]


# --- an installed workflow's command with no declaring agent (the un-migrated-config case) --------


async def test_an_undeclared_workflow_command_gets_an_actionable_reply_and_starts_no_turn(tmp_path):
    """The finding this guards against: a config that predates naming "planning" in
    [agents.assistant].tools must not let /plan fall through to a plain turn. That path would run the
    model on the literal "/plan do the thing" string and `_persist` would write that literal string into
    the saved transcript as the user's message -- disagreeing with the web UI's Plan toggle, whose chat
    bubble shows the user's own words. Proven here by leaving a real turn's history untouched by the
    /plan attempt that follows it, in the same conversation.
    """
    cfg = _config(tmp_path)
    channel1 = FakeChannel()
    assistant1 = await Assistant.create(cfg, channel1, client=MockAsyncModelClient(["hi there"]))
    await assistant1._handle(ChannelMessage(text="hi"), conversation_id=assistant1._active_id)
    before = [dict(m) for m in assistant1._store.get(assistant1._active_id).messages]
    assistant1._store.close()  # flush TinyDB before assistant2 reopens the same file

    agents = example_agents()
    agents["assistant"].tools = [name for name in agents["assistant"].tools if name != "planning"]
    channel2 = FakeChannel(inbound=["/plan do the thing"])
    assistant2 = await Assistant.create(
        _config(tmp_path, agents=agents), channel2, client=MockAsyncModelClient(["should not run"])
    )

    await _run_one_message(assistant2, channel2)

    assert channel2.sent == [
        "The 'planning' toolset offers /plan, but no agent declares it. Add 'planning' to "
        "[agents.assistant].tools in your config.toml."
    ]
    after = [dict(m) for m in assistant2._store.get(assistant2._active_id).messages]
    assert after == before
