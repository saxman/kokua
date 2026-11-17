"""What `Assistant.create` does, and what it deliberately leaves to `Assistant.start`.

The split exists so a front end can paint a conversation list without waiting on a remote MCP
handshake. These tests pin both halves: that `create` reaches no network and builds no agent, and
that `start` does both and can be called twice safely.
"""

from __future__ import annotations

import asyncio

import pytest

from kokua.config import ConfigError, MCPServerConfig
from kokua.core.assistant import Assistant
from tests.channels import FakeChannel, example_agents, planning_settings
from tests.helpers import MockAsyncModelClient

from kokua.config import AssistantConfig


def _config(tmp_path, **overrides) -> AssistantConfig:
    base = {
        "data_dir": tmp_path,
        "agents": example_agents(),
        "entry_agent": "assistant",
        "toolset_settings": planning_settings(),
    }
    base.update(overrides)
    return AssistantConfig(**base)


class _FakeClient:
    def __init__(self, tool_names=()):
        self._tool_names = list(tool_names)

    async def as_tools(self):
        def named(name):
            def fn():
                return None

            fn.__name__ = name
            return fn

        return [named(name) for name in self._tool_names]

    async def aclose(self):
        return None


async def test_create_connects_no_mcp_server(tmp_path, monkeypatch):
    """The reason the split exists: nothing in `create` may wait on a remote endpoint."""
    from kokua.mcp import servers

    calls = []

    async def fake_connect(url, **kw):
        calls.append(url)
        return _FakeClient(["remote_tool"]), "none"

    monkeypatch.setattr(servers, "connect_mcp", fake_connect)
    cfg = _config(tmp_path, mcp_servers=[MCPServerConfig(url="https://svc/mcp", name="svc")])

    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    assert calls == []
    assert assistant._mcp_servers == []


async def test_start_connects_the_configured_servers(tmp_path, monkeypatch):
    from kokua.mcp import servers

    async def fake_connect(url, **kw):
        return _FakeClient(["remote_tool"]), "none"

    monkeypatch.setattr(servers, "connect_mcp", fake_connect)
    cfg = _config(tmp_path, mcp_servers=[MCPServerConfig(url="https://svc/mcp", name="svc")])

    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    assert [c.url for c in assistant._mcp_servers] == ["https://svc/mcp"]


async def test_start_twice_connects_once(tmp_path, monkeypatch):
    """`run` calls it too, so a front end that called it itself must not pay twice: a second connect
    would append the same server to the live connection list a second time."""
    from kokua.mcp import servers

    calls = []

    async def fake_connect(url, **kw):
        calls.append(url)
        return _FakeClient(["remote_tool"]), "none"

    monkeypatch.setattr(servers, "connect_mcp", fake_connect)
    cfg = _config(tmp_path, mcp_servers=[MCPServerConfig(url="https://svc/mcp", name="svc")])

    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    await assistant.start()

    assert calls == ["https://svc/mcp"]
    assert len(assistant._mcp_servers) == 1


async def test_a_failed_start_does_not_retry(tmp_path, monkeypatch):
    """`_started` is set before the body runs, so a failed start is terminal rather than retryable.

    A bad `confirm_tools` entry is a config mistake the user has to fix, not a transient worth retrying:
    `validate_confirm_tools` runs after the MCP reconnect, so the connect has already happened by the
    time the error is raised. If a second `start()` retried, it would reach `reconnect_mcp_servers`
    again and append the same server to `_mcp_servers` a second time, since a fresh connection is not
    deduplicated against one already in the list.
    """
    from kokua.mcp import servers

    calls = []

    async def fake_connect(url, **kw):
        calls.append(url)
        return _FakeClient(["remote_tool"]), "none"

    monkeypatch.setattr(servers, "connect_mcp", fake_connect)
    cfg = _config(
        tmp_path,
        mcp_servers=[MCPServerConfig(url="https://svc/mcp", name="svc")],
        default_confirm=["execute_pythn"],
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    with pytest.raises(ConfigError):
        await assistant.start()

    assert calls == ["https://svc/mcp"]  # the connect already happened before the gate check failed

    await assistant.start()  # does not raise again, and does not retry the connect

    assert calls == ["https://svc/mcp"]
    assert len(assistant._mcp_servers) == 1


async def test_create_builds_no_agent_and_start_does(tmp_path):
    """A built agent means a resolved model, which on a real config is a further second of startup."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assert assistant._registry.live_agents() == []

    await assistant.start()

    assert assistant._registry.live_agents() != []


async def test_start_validates_the_approval_gates(tmp_path):
    """`confirm_tools` can only be checked once every tool exists, which is now a `start` concern.
    A gate naming no real tool is a hard error rather than a prompt that never comes."""
    assistant = await Assistant.create(
        _config(tmp_path, default_confirm=["execute_pythn"]), FakeChannel(), client=MockAsyncModelClient([])
    )

    with pytest.raises(ConfigError) as error:
        await assistant.start()

    assert "execute_pythn" in str(error.value)


async def test_run_starts_an_assistant_that_was_never_started(tmp_path):
    """The bound on the two-phase hazard: serving implies started, so the only way to hold an
    unstarted assistant is to deliberately never serve it."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    # Bounded, like the only other tests that drive `run()` (tests/core/test_turns.py): `run()` joins a
    # TaskGroup that includes the scheduler, and there is no pytest-timeout here, so an unbounded await
    # on a serve loop that failed to come down would hang the suite rather than fail one test.
    await asyncio.wait_for(assistant.run(), timeout=5.0)

    assert assistant._registry.live_agents() != []


def test_booting_the_shipped_config_imports_neither_torch_nor_sentence_transformers(tmp_path):
    """Constructing an `hf:` embedding client imports AIMU's HF provider, which imports torch: seconds
    added to every startup. Startup must not build the client, so a bad `[documents].embedding_model`
    is the first documents build's error instead. Run in a subprocess, so an import made earlier in this
    process cannot mask a regression; it uses the real `embedding_client_for`, not the suite's stub."""
    import os
    import subprocess
    import sys
    import textwrap

    home = tmp_path / "home"
    home.mkdir()
    script = textwrap.dedent(
        """
        import asyncio, sys
        from kokua import cli
        from kokua.config import file as settings
        from tests.channels import FakeChannel

        (cli.settings.resolve_path(None)[0]).write_text(settings.example_text(), encoding="utf-8")
        config = cli.resolve_config(cli.build_arg_parser().parse_args([]))
        from kokua.core.assistant import Assistant

        asyncio.run(Assistant.create(config, FakeChannel()))
        heavy = sorted(m for m in ("torch", "sentence_transformers") if m in sys.modules)
        print("HEAVY:" + ",".join(heavy))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "KOKUA_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "HEAVY:"
