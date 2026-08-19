"""The /diag command."""

from __future__ import annotations

import asyncio


from aimu.aio.channels.base import Channel, ChannelMessage

from kokua.core.assistant import Assistant
from tests.channels import _config
from tests.fakes import _BlockingStreamClient
from tests.helpers import MockAsyncModelClient


async def test_diag_command_does_not_start_a_turn(tmp_path):
    class _DiagOnly(Channel):
        name = "fake"

        def __init__(self):
            self.sent: list[str] = []

        async def receive(self):
            yield ChannelMessage(text="/diag", channel="fake")

        async def send(self, content, *, reply_to=None):
            if isinstance(content, str):
                self.sent.append(content)
            else:
                async for _ in content:
                    pass

    channel = _DiagOnly()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    await assistant._serve_channel()
    assert assistant._tracker.get(assistant._active_id) is None  # /diag must not dispatch a turn
    assert any("turn in flight: no" in s.lower() for s in channel.sent)


class _DiagChannel(Channel):
    """Yields a normal message, waits until the turn is running, then yields '/diag'."""

    name = "fake"

    def __init__(self, started):
        self._started = started
        self.sent: list[str] = []

    async def receive(self):
        yield ChannelMessage(text="long task", channel="fake")
        await self._started.wait()
        yield ChannelMessage(text="/diag", channel="fake")

    async def send(self, content, *, reply_to=None):
        if isinstance(content, str):
            self.sent.append(content)
            return
        async for _ in content:
            pass


async def test_diag_reports_wedged_turn_with_stack(tmp_path):
    client = _BlockingStreamClient()
    channel = _DiagChannel(client.started)
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._serve_channel()  # starts the hung turn, then answers /diag while it is wedged
    report = "\n".join(channel.sent)
    assert "turn in flight: yes" in report.lower()
    assert "active turns: 1" in report.lower()
    assert "stuck turn stack" in report.lower()

    # cleanup: cancel the hung turn
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:
        info.handle.cancel()
        await asyncio.gather(info.handle.task, return_exceptions=True)


async def test_diag_reports_the_model_each_agent_runs_on(tmp_path):
    """The model is not in the settings panel any more, so /diag is where a running session says which
    one it is -- and with per-agent overrides, one line for the default is not the whole answer."""
    from tests.channels import FakeChannel, example_agents

    agents = example_agents()
    agents["researcher"].model = "ollama:qwen3:32b"
    config = _config(tmp_path, model="ollama:qwen3:8b", agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))

    assert "- model: ollama:qwen3:8b | researcher: ollama:qwen3:32b" in assistant._diag_report()


async def test_diag_names_the_model_aimu_resolved_when_the_config_declares_none(tmp_path):
    from kokua.core.build import model_label

    class _Client:
        model = "ollama:auto-resolved"

    config = _config(tmp_path, model=None)
    assert model_label(config, config.entry_agent, _Client()) == "ollama:auto-resolved"


async def test_diag_reports_the_thinking_each_agent_runs_at(tmp_path):
    """Reasoning effort is startup-only with no panel field, exactly like the model, so /diag is the
    only place a running session says what it resolved to."""
    from tests.channels import FakeChannel, example_agents

    agents = example_agents()
    agents["researcher"].thinking = "high"
    agents["coder"].thinking = False
    config = _config(tmp_path, thinking="low", agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))

    assert "- thinking: low | coder: off | researcher: high" in assistant._diag_report()


async def test_diag_omits_the_thinking_line_when_nothing_is_declared(tmp_path):
    """The common case emits nothing, so a line reading 'thinking: unset' would be noise on every /diag."""
    from tests.channels import FakeChannel

    config = _config(tmp_path)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))

    assert "thinking:" not in assistant._diag_report()


async def test_diag_reports_a_default_of_off_with_no_overrides(tmp_path):
    from tests.channels import FakeChannel

    config = _config(tmp_path, thinking=False)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))

    assert "- thinking: off" in assistant._diag_report()


async def test_diag_reports_the_generation_parameters_each_agent_runs_with(tmp_path):
    """Startup-only with no panel field, exactly like the model and the effort, so /diag is the only
    place a running session says what it is sampling at."""
    from tests.channels import FakeChannel, example_agents

    agents = example_agents()
    agents["researcher"].generation = {"temperature": 0.2}
    config = _config(tmp_path, generation={"temperature": 0.7, "context_length": 32768}, agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))

    assert (
        "- generation: context_length=32768, temperature=0.7 | researcher: temperature=0.2" in assistant._diag_report()
    )


async def test_diag_omits_the_generation_line_when_nothing_is_declared(tmp_path):
    """The common case is every parameter at the model card's own value, so a line saying so is noise."""
    from tests.channels import FakeChannel

    config = _config(tmp_path)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))

    assert "generation:" not in assistant._diag_report()
