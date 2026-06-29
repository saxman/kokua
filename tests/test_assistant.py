"""Mock-only tests for the Kokua assistant core and CLI parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

import kokua.paths
from helpers import MockAsyncModelClient
from kokua.assistant import Assistant
from kokua.cli import build_arg_parser, resolve_config
from kokua.config import AssistantConfig

from aimu.aio.channels.base import Channel, ChannelMessage
from aimu.models import StreamingContentType


class FakeChannel(Channel):
    name = "fake"

    def __init__(self, inbound: list[str] | None = None):
        self._inbound = inbound or []
        self.sent: list[str] = []

    async def receive(self) -> AsyncIterator[ChannelMessage]:
        for text in self._inbound:
            yield ChannelMessage(text=text, sender="fake", channel="fake")

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
        # All leaf paths derive from data_dir; point it at the test's tmp dir.
        "data_dir": tmp_path,
        # Memory is on by default in real runs, but off here so the bulk of the tests stay fast and
        # hermetic (no ChromaDB init / state writes). The memory tests opt in with memory=True.
        "memory": False,
    }
    base.update(overrides)
    return AssistantConfig(**base)


def test_arg_parser_defaults():
    args = build_arg_parser().parse_args([])
    assert args.model is None
    assert args.config is None
    assert args.frontend is None  # resolve_config falls back to the "cli" default
    assert args.reminder_seconds is None


def test_default_config_lives_under_state_dir():
    cfg = resolve_config(build_arg_parser().parse_args([]))
    assert cfg.data_dir == kokua.paths.data_dir()
    assert cfg.skills_dir == kokua.paths.skills_dir()
    assert cfg.history_path == str(kokua.paths.history_path())
    assert cfg.frontend == "cli"


def test_arg_parser_overrides():
    args = build_arg_parser().parse_args(
        [
            "--model",
            "anthropic:claude-sonnet-4-6",
            "--reminder-seconds",
            "5",
            "--frontend",
            "web",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
        ]
    )
    cfg = resolve_config(args)
    assert cfg.model == "anthropic:claude-sonnet-4-6"
    assert cfg.reminder_seconds == 5.0
    assert cfg.frontend == "web"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000


def test_default_tools_groups():
    assert AssistantConfig().tools == ["web", "fs", "compute", "misc"]
    assert resolve_config(build_arg_parser().parse_args([])).tools == ["web", "fs", "compute", "misc"]


def test_tools_flag_parses_groups():
    assert resolve_config(build_arg_parser().parse_args(["--tools", "web, misc"])).tools == ["web", "misc"]
    assert resolve_config(build_arg_parser().parse_args(["--tools", "none"])).tools == ["none"]


async def test_assistant_wires_builtin_tools_by_default(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    # Default groups are present...
    assert {"get_weather", "read_file", "calculate", "get_current_date_and_time"} <= names
    # ...and the generative groups (opt-in, need AIMU_*_MODEL) are not.
    assert "generate_image" not in names


async def test_assistant_tools_none_omits_builtins(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path, tools=["none"]), FakeChannel(), client=MockAsyncModelClient([])
    )
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "get_weather" not in names and "calculate" not in names
    # The assistant's own tools remain.
    assert {"author_skill", "add_skill_script", "add_mcp_server"} <= names


async def test_assistant_unknown_tool_group_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown tool group"):
        await Assistant.create(_config(tmp_path, tools=["bogus"]), FakeChannel(), client=MockAsyncModelClient([]))


async def test_assistant_handles_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Sure, done."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(ChannelMessage(text="do a thing", channel="fake"))

    assert channel.sent == ["Sure, done."]
    assert assistant._conversation.messages  # persisted at least the turn


async def test_assistant_proactive_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Don't forget lunch."])
    assistant = await Assistant.create(_config(tmp_path, reminder_text="remind"), channel, client=client)

    await assistant._proactive()

    assert channel.sent == ["Don't forget lunch."]


async def test_assistant_persists_and_restores(tmp_path):
    cfg = _config(tmp_path)

    channel1 = FakeChannel()
    client1 = MockAsyncModelClient(["first reply"])
    assistant1 = await Assistant.create(cfg, channel1, client=client1)
    await assistant1._handle(ChannelMessage(text="remember this"))
    assistant1._conversation.close()  # flush TinyDB

    channel2 = FakeChannel()
    client2 = MockAsyncModelClient([])  # no turn; just restore
    assistant2 = await Assistant.create(cfg, channel2, client=client2)

    restored = [m.get("content") for m in assistant2._agent.model_client.messages]
    assert "remember this" in restored
    assert "first reply" in restored


async def test_assistant_wires_author_skill_tool(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    tools = assistant._agent.tools
    author = next((t for t in tools if t.__name__ == "author_skill"), None)
    assert author is not None and author.__tool_is_async__ is True

    await author(name="format-standup", description="Format a standup update.", body="# Standup\n\nDo X.")
    assert (cfg.skills_dir / "format-standup" / "SKILL.md").exists()
    assert "format-standup" in assistant._agent.skill_manager.skills


# --- MCP server wiring (startup flag + runtime tool) -----------------------------------------


def _fake_mcp_tool(name: str):
    async def fn(**kwargs):
        return "ok"

    fn.__name__ = name
    fn.__tool_spec__ = {"function": {"name": name}}
    fn.__tool_is_async__ = True
    fn.__tool_is_streaming__ = False
    return fn


class _FakeMCP:
    def __init__(self, tools):
        self._tools = tools
        self.closed = False

    async def as_tools(self):
        return self._tools

    async def aclose(self):
        self.closed = True


async def test_startup_mcp_servers_wire_tools(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        assert auth == "tok"
        return _FakeMCP([_fake_mcp_tool("remote_search"), _fake_mcp_tool("remote_fetch")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(
        _config(tmp_path, mcp_servers=["https://svc/mcp"], mcp_bearer="tok"),
        FakeChannel(),
        client=MockAsyncModelClient([]),
    )
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert {"remote_search", "remote_fetch"} <= names
    assert len(assistant._mcp_clients) == 1


async def test_startup_mcp_connect_failure_does_not_crash(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(
        _config(tmp_path, mcp_servers=["https://down/mcp"]), FakeChannel(), client=MockAsyncModelClient([])
    )
    assert assistant._mcp_clients == []


async def test_add_mcp_server_tool_adds_tools_at_runtime(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")
    assert add_mcp.__tool_is_async__ is True

    msg = await add_mcp(url="https://svc/mcp")
    assert "remote_search" in msg
    assert "remote_search" in {fn.__name__ for fn in assistant._agent.tools}
    assert len(assistant._mcp_clients) == 1

    msg2 = await add_mcp(url="https://svc/mcp")
    assert "no new tools" in msg2
    assert [fn.__name__ for fn in assistant._agent.tools].count("remote_search") == 1


async def test_add_mcp_server_tool_reports_connect_failure(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")

    msg = await add_mcp(url="https://down/mcp")
    assert "Failed to connect" in msg and "boom" in msg
    assert assistant._mcp_clients == []


# --- Memory (facts + documents) --------------------------------------------------------------

_MEMORY_TOOL_NAMES = {
    "store_memory",
    "search_memories",
    "list_memories",
    "save_document",
    "read_document",
    "list_documents",
    "search_documents",
}


async def test_memory_wires_both_stores(tmp_path):
    assistant = await Assistant.create(_config(tmp_path, memory=True), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert _MEMORY_TOOL_NAMES <= names
    assert assistant._memory_store is not None
    assert assistant._document_store is not None


async def test_no_memory_omits_tools_and_stores(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert _MEMORY_TOOL_NAMES.isdisjoint(names)
    assert assistant._memory_store is None
    assert assistant._document_store is None


def test_memory_flag_parses():
    assert resolve_config(build_arg_parser().parse_args([])).memory is True
    assert resolve_config(build_arg_parser().parse_args(["--no-memory"])).memory is False


async def test_document_tools_round_trip(tmp_path):
    """The document tools are wired to a working DocumentStore (pure-Python, hermetic)."""
    assistant = await Assistant.create(_config(tmp_path, memory=True), FakeChannel(), client=MockAsyncModelClient([]))
    tools = {fn.__name__: fn for fn in assistant._agent.tools}
    assert tools["save_document"]("/notes/standup.md", "Yesterday, Today, Blockers") == "Saved /notes/standup.md."
    assert tools["read_document"]("/notes/standup.md") == "Yesterday, Today, Blockers"


# --- /stop cancellation -----------------------------------------------------------------------


class _BlockingStreamClient(MockAsyncModelClient):
    """Records the user turn, signals it started, then hangs until the turn task is cancelled."""

    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        self.messages.append({"role": "user", "content": user_message})
        self.started.set()
        await asyncio.Event().wait()  # hang until cancelled


class _StopChannel(Channel):
    """Yields a normal message, waits until the turn is running, then yields '/stop'."""

    name = "fake"

    def __init__(self, started):
        self._started = started
        self.sent: list[str] = []

    async def receive(self):
        yield ChannelMessage(text="long task", channel="fake")
        await self._started.wait()
        yield ChannelMessage(text="/stop", channel="fake")

    async def send(self, content, *, reply_to=None):
        if isinstance(content, str):
            self.sent.append(content)
            return
        async for _ in content:  # consume the stream; this is what /stop cancels
            pass


async def test_stop_cancels_in_flight_turn(tmp_path):
    client = _BlockingStreamClient()
    channel = _StopChannel(client.started)
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._serve_channel()  # reads "long task" (starts the turn), then "/stop" (cancels it)
    if assistant._current is not None:  # let the cancelled turn finish its (stopped) + persist
        await asyncio.gather(assistant._current.task, return_exceptions=True)

    assert "(stopped)" in channel.sent
    # The partial turn was captured for resume (the agent snapshots in its finally).
    assert any(m.get("content") == "long task" for m in assistant._agent.model_client.messages)


async def test_stop_with_no_active_turn_is_noop(tmp_path):
    class _OnlyStop(Channel):
        name = "fake"

        async def receive(self):
            yield ChannelMessage(text="/stop", channel="fake")

        async def send(self, content, *, reply_to=None):
            pass

    assistant = await Assistant.create(_config(tmp_path), _OnlyStop(), client=MockAsyncModelClient([]))
    await assistant._serve_channel()  # must not raise with no in-flight turn
    assert assistant._current is None


async def test_assistant_authors_and_registers_runnable_script(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    tools = assistant._agent.tools
    author = next(t for t in tools if t.__name__ == "author_skill")
    add_script = next(t for t in tools if t.__name__ == "add_skill_script")
    assert add_script.__tool_is_async__ is True

    await author(name="disk", description="Disk helpers.", body="# Disk")
    msg = await add_script(skill_name="disk", filename="usage.py", content="print('disk ok')\n")

    assert "disk__usage" in msg
    assert (cfg.skills_dir / "disk" / "scripts" / "usage.py").exists()
    # reload_skills() ran, so the new script tool is callable on the live client.
    assert "disk__usage" in [fn.__name__ for fn in assistant._agent.model_client.tools]
