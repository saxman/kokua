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
