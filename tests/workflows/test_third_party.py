"""A workflow Kokua does not ship, reached the way a third party's would be.

The point is that nothing in this test imports a Kokua base class or touches the core: it declares a
Toolset carrying a Workflow whose runner is one of AIMU's own, registers it through the same registry
every provider uses, and runs a turn through the resulting command.
"""

from __future__ import annotations

from pathlib import Path

from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamChunk, StreamingContentType

from kokua.config import AssistantConfig
from kokua.core.assistant import Assistant
from kokua.toolsets.agents import build_command_map
from kokua.toolsets.registry import Toolset, register
from kokua.workflows import Workflow
from tests.channels import FakeChannel, example_agents
from tests.helpers import MockAsyncModelClient


class _Echo:
    """Stands in for an AIMU workflow: a bare AsyncRunner with no Kokua knowledge.

    Its streaming path yields the whole answer as one chunk, which is exactly how AIMU's
    ``PlanExecuteEvaluator`` degrades, so this also pins that the base tier tolerates it.
    """

    async def run(self, task, generate_kwargs=None, stream=False, images=None):
        answer = f"summarised: {task}"
        if not stream:
            return answer

        async def one_chunk():
            yield StreamChunk(StreamingContentType.GENERATING, answer, agent="summarise", iteration=0)

        return one_chunk()

    @property
    def messages(self):
        return {}


THIRD_PARTY = Toolset(
    name="summarise",
    description="Summarise a request.",
    build=lambda ctx: [],
    workflow=Workflow(
        name="summarise",
        description="Summarise.",
        command="sum",
        usage="/sum <text>",
        build=lambda ctx: _Echo(),
    ),
)


def test_a_third_party_workflow_earns_its_command_by_declaration():
    agents = example_agents()
    agents["assistant"].tools = ["summarise"]
    config = AssistantConfig(agents=agents, entry_agent="assistant")

    commands = build_command_map(config, register([("plugin", [THIRD_PARTY])]))

    assert set(commands) == {"sum"}
    assert commands["sum"].usage == "/sum <text>"


async def test_a_third_party_workflow_runs_a_turn(tmp_path: Path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        AssistantConfig(data_dir=tmp_path, agents=example_agents(), entry_agent="assistant"),
        channel,
        client=MockAsyncModelClient(["unused"]),
    )

    await assistant._handle(
        ChannelMessage(text="a long document", channel="fake"),
        conversation_id=assistant._active_id,
        workflow=THIRD_PARTY.workflow,
    )

    assert channel.sent == ["summarised: a long document"]
