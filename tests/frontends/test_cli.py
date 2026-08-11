"""The terminal front end. Only its error path was covered before."""

from __future__ import annotations

from pathlib import Path

import pytest

from kokua.config import AssistantConfig
from kokua.frontends import cli as cli_frontend
from kokua.frontends.cli import FRONTEND
from tests.channels import example_subagent_roles
from tests.helpers import MockAsyncModelClient


def _config(tmp_path: Path) -> AssistantConfig:
    return AssistantConfig(data_dir=tmp_path, memory=False, subagent_roles=example_subagent_roles())


def test_frontend_is_registered_with_a_name_and_description():
    assert FRONTEND.name == "cli"
    assert FRONTEND.description and FRONTEND.run is cli_frontend.run


async def test_run_builds_the_assistant_and_serves_until_the_channel_closes(tmp_path, monkeypatch, capsys):
    """The happy path: a startup notice on stderr, then serve, then return cleanly on close."""
    served = []

    class _ImmediatelyClosingChannel:
        name = "cli"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def receive(self):
            return
            yield  # pragma: no cover -- makes this an async generator

        async def send(self, content, *, reply_to=None):
            pass

    monkeypatch.setattr(cli_frontend, "CLIChannel", _ImmediatelyClosingChannel)

    real_create = cli_frontend.Assistant.create

    async def create(config, channel, **kwargs):
        assistant = await real_create(config, channel, client=MockAsyncModelClient([]))
        served.append(assistant)
        return assistant

    monkeypatch.setattr(cli_frontend.Assistant, "create", create)

    await cli_frontend.run(_config(tmp_path), args=None)

    assert served, "the front end never built an assistant"
    assert "[notice]" in capsys.readouterr().err  # the no-sandbox warning reaches the user


async def test_run_passes_the_display_flags_to_the_channel(tmp_path, monkeypatch):
    captured = {}

    class _Channel:
        name = "cli"

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def receive(self):
            return
            yield  # pragma: no cover

        async def send(self, content, *, reply_to=None):
            pass

    monkeypatch.setattr(cli_frontend, "CLIChannel", _Channel)

    real_create = cli_frontend.Assistant.create
    monkeypatch.setattr(
        cli_frontend.Assistant,
        "create",
        lambda config, channel, **kw: real_create(config, channel, client=MockAsyncModelClient([])),
    )

    config = AssistantConfig(
        data_dir=tmp_path,
        memory=False,
        show_thinking=False,
        show_tools=True,
        subagent_roles=example_subagent_roles(),
    )
    await cli_frontend.run(config, args=None)

    assert captured == {"show_thinking": False, "show_tools": True}


async def test_run_reports_an_unbuildable_model_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    async def explode(*args, **kwargs):
        raise cli_frontend.ModelClientError("no model configured")

    monkeypatch.setattr(cli_frontend.Assistant, "create", explode)

    with pytest.raises(SystemExit) as exit_info:
        await cli_frontend.run(_config(tmp_path), args=None)

    assert exit_info.value.code == 1
    assert "no model configured" in capsys.readouterr().err
