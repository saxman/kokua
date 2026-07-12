"""Mock-only tests for the Kokua assistant core and CLI parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

import kokua.paths
from helpers import MockAsyncModelClient
from kokua import runtime_settings
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
    assert cfg.sessions_path == kokua.paths.sessions_path()
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


def test_sessions_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path, memory=False)
    assert cfg.sessions_path == tmp_path / "sessions.json"


def test_paths_sessions_path_is_data_dir_leaf():
    assert kokua.paths.sessions_path() == kokua.paths.data_dir() / "sessions.json"


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


async def test_assistant_wires_subagent_tool_by_default(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "spawn_subagent" in names


async def test_assistant_subagents_flag_omits_tool(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path, subagents=False), FakeChannel(), client=MockAsyncModelClient([])
    )
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "spawn_subagent" not in names


async def test_assistant_unknown_tool_group_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown tool group"):
        await Assistant.create(_config(tmp_path, tools=["bogus"]), FakeChannel(), client=MockAsyncModelClient([]))


async def test_assistant_handles_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Sure, done."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(ChannelMessage(text="do a thing", channel="fake"))

    assert channel.sent == ["Sure, done."]
    assert assistant.history  # persisted at least the turn


async def test_assistant_proactive_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Don't forget lunch."])
    assistant = await Assistant.create(_config(tmp_path, reminder_text="remind"), channel, client=client)

    await assistant._proactive()

    assert channel.sent == ["Don't forget lunch."]


async def test_assistant_proactive_tags_turn_provenance(tmp_path):
    from aimu.models import PROVENANCE_KEY, PROVENANCE_PROACTIVE

    channel = FakeChannel()
    client = MockAsyncModelClient(["Time for a walk."])
    assistant = await Assistant.create(_config(tmp_path, reminder_text="remind"), channel, client=client)

    await assistant._proactive()

    tagged = [m.get(PROVENANCE_KEY) for m in assistant._agent.model_client.messages]
    assert PROVENANCE_PROACTIVE in tagged
    assert all(p in (None, PROVENANCE_PROACTIVE) for p in tagged)


async def test_assistant_persists_and_restores(tmp_path):
    cfg = _config(tmp_path)

    channel1 = FakeChannel()
    client1 = MockAsyncModelClient(["first reply"])
    assistant1 = await Assistant.create(cfg, channel1, client=client1)
    await assistant1._handle(ChannelMessage(text="remember this"))
    assistant1._store.close()  # flush TinyDB

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
    assert len(assistant._mcp_servers) == 1


async def test_startup_mcp_connect_failure_does_not_crash(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(
        _config(tmp_path, mcp_servers=["https://down/mcp"]), FakeChannel(), client=MockAsyncModelClient([])
    )
    assert assistant._mcp_servers == []


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
    assert len(assistant._mcp_servers) == 1

    msg2 = await add_mcp(url="https://svc/mcp")
    assert "Already connected" in msg2
    assert len(assistant._mcp_servers) == 1
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
    assert assistant._mcp_servers == []


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


# --- Multiple conversations -------------------------------------------------------------------


async def test_turn_persists_to_active_session_with_title(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Sure."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(ChannelMessage(text="plan my trip to Kauai", channel="fake"))

    stored = assistant._store.get(assistant._session.key)
    assert any(m.get("content") == "plan my trip to Kauai" for m in stored.messages)
    assert stored.metadata["title"] == "plan my trip to Kauai"


async def test_history_returns_active_session_messages(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))
    await assistant._handle(ChannelMessage(text="hello", channel="fake"))
    assert assistant.history == assistant._session.messages
    assert any(m.get("content") == "hello" for m in assistant.history)


async def test_fresh_start_has_empty_active_session(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant._session.messages == []
    assert assistant._store.list_keys() == [assistant._session.key]


class _ConvCapturingChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.conversation_pushes: list[list] = []

    async def send_conversations(self, items):
        self.conversation_pushes.append(items)


async def test_first_turn_pushes_conversations(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["a", "b"]))

    await assistant._handle(ChannelMessage(text="hello there", channel="fake"))
    assert len(channel.conversation_pushes) == 1  # title just set -> one push
    assert channel.conversation_pushes[0][0]["title"] == "hello there"

    await assistant._handle(ChannelMessage(text="again", channel="fake"))
    assert len(channel.conversation_pushes) == 1  # title already set -> no further push


async def test_list_conversations_recency_desc(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a", "b"]))
    await assistant._handle(ChannelMessage(text="first chat", channel="fake"))
    first_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="second chat", channel="fake"))
    second_id = assistant._session.key

    items = assistant.list_conversations()
    assert [i["id"] for i in items] == [second_id, first_id]  # most recent first
    assert items[0]["title"] == "second chat"
    assert items[0]["active"] is True and items[1]["active"] is False


async def test_new_conversation_resets_agent(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a"]))
    await assistant._handle(ChannelMessage(text="old chat", channel="fake"))
    assert assistant._agent.model_client.messages  # has the old turn

    new_id = await assistant.new_conversation()
    assert assistant._session.key == new_id
    assert assistant._session.messages == []
    assert assistant._agent.model_client.messages == []  # restore([]) cleared it


async def test_select_conversation_restores_messages(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a"]))
    await assistant._handle(ChannelMessage(text="keep me", channel="fake"))
    first_id = assistant._session.key
    await assistant.new_conversation()
    assert not any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)

    await assistant.select_conversation(first_id)
    assert assistant._session.key == first_id
    assert any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)


async def test_delete_inactive_conversation_leaves_active(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a", "b"]))
    await assistant._handle(ChannelMessage(text="old chat", channel="fake"))
    old_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="current chat", channel="fake"))
    active_id = assistant._session.key

    await assistant.delete_conversation(old_id)
    assert old_id not in assistant._store.list_keys()
    assert assistant._session.key == active_id  # active unchanged
    assert any(m.get("content") == "current chat" for m in assistant._agent.model_client.messages)


async def test_delete_active_switches_to_most_recent_remaining(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a", "b"]))
    await assistant._handle(ChannelMessage(text="keep me", channel="fake"))
    keep_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="delete me", channel="fake"))
    delete_id = assistant._session.key

    await assistant.delete_conversation(delete_id)
    assert delete_id not in assistant._store.list_keys()
    assert assistant._session.key == keep_id  # switched to the remaining one
    assert any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)


async def test_delete_last_conversation_creates_fresh_empty(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a"]))
    await assistant._handle(ChannelMessage(text="only chat", channel="fake"))
    only_id = assistant._session.key

    await assistant.delete_conversation(only_id)
    assert only_id not in assistant._store.list_keys()
    assert assistant._session.key != only_id  # a fresh, empty active conversation
    assert assistant._session.messages == []
    assert assistant._agent.model_client.messages == []


# --- Tool approval ----------------------------------------------------------------------------


def test_default_confirm_tools():
    assert AssistantConfig().confirm_tools == ["add_skill_script", "add_mcp_server", "execute_python"]
    assert resolve_config(build_arg_parser().parse_args([])).confirm_tools == [
        "add_skill_script",
        "add_mcp_server",
        "execute_python",
    ]


def test_confirm_tools_flag_parses():
    cfg = resolve_config(build_arg_parser().parse_args(["--confirm-tools", "add_skill_script, execute_python"]))
    assert cfg.confirm_tools == ["add_skill_script", "execute_python"]


def test_confirm_tools_flag_empty_disables():
    assert resolve_config(build_arg_parser().parse_args(["--confirm-tools", ""])).confirm_tools == []


async def test_assistant_wires_approval_policy(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant._agent.tool_approval == assistant._approve


async def test_approve_allows_ungated_tool_without_prompting(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    assert await assistant._approve("get_weather", {}) is True
    assert channel.sent == []  # no prompt for an ungated tool


async def test_approve_gated_tool_waits_for_routed_answer(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    task = asyncio.create_task(assistant._approve("add_skill_script", {"skill_name": "x"}))
    await asyncio.sleep(0)  # let the policy register the pending approval and prompt
    assert assistant._pending_approval is not None
    assert channel.sent  # a prompt was sent to the user
    assistant._pending_approval.set_result(True)
    assert await task is True


async def test_approve_proactive_auto_denies_gated_tool(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    assistant._in_proactive = True
    assert await assistant._approve("add_skill_script", {}) is False
    assert channel.sent == []  # auto-deny: no prompt, no waiting


async def test_serve_loop_routes_message_to_pending_approval(tmp_path):
    class _OneMsg(Channel):
        name = "fake"

        async def receive(self):
            yield ChannelMessage(text="y", channel="fake")

        async def send(self, content, *, reply_to=None):
            pass

    assistant = await Assistant.create(_config(tmp_path), _OneMsg(), client=MockAsyncModelClient([]))
    fut = asyncio.get_running_loop().create_future()
    assistant._pending_approval = fut

    await assistant._serve_channel()

    assert fut.done() and fut.result() is True
    assert assistant._current is None  # the answer did not start a new turn


async def test_denied_gated_tool_does_not_run(tmp_path):
    cfg = _config(tmp_path, confirm_tools=["add_skill_script"])
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    # Proactive context makes _approve auto-deny without an interactive prompt, so the real dispatch
    # path can be exercised synchronously.
    assistant._in_proactive = True
    agent = assistant._agent
    agent._prepare_run()  # publish tool_approval + tools onto the client (as a run would)

    await agent.model_client._handle_tool_calls(
        [{"name": "add_skill_script", "arguments": {"skill_name": "disk", "filename": "u.py", "content": "print(1)\n"}}]
    )

    assert agent.model_client.messages[-1]["content"] == "Tool 'add_skill_script' was not approved."
    assert not (cfg.skills_dir / "disk" / "scripts" / "u.py").exists()


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


async def test_add_mcp_server_auto_oauth_on_auth_challenge(tmp_path, monkeypatch):
    """A tokenless connect that hits a 401 transparently retries with a ChatOAuth provider."""
    from aimu import aio

    from kokua.mcp_auth import ChatOAuth

    attempts = []

    async def fake_connect(*, url=None, auth=None, **kw):
        attempts.append(auth)
        if auth is None:  # first, unauthenticated attempt -> server challenges
            raise RuntimeError("failed to connect: Client error '401 Unauthorized'")
        return _FakeMCP([_fake_mcp_tool("remote_trade")])  # the OAuth-provider attempt succeeds

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")

    msg = await add_mcp(url="https://svc/mcp")  # no bearer token -> auto OAuth on the 401
    assert attempts[0] is None  # first attempt unauthenticated
    assert isinstance(attempts[1], ChatOAuth)  # retried with a chat-link OAuth provider
    assert "remote_trade" in msg
    assert "remote_trade" in {fn.__name__ for fn in assistant._agent.tools}
    # Tokens persist under the app data dir so a later reconnect is silent.
    assert (tmp_path / "mcp-oauth").exists()


async def test_add_mcp_server_no_oauth_on_non_auth_failure(tmp_path, monkeypatch):
    """A non-auth failure (unreachable host) is reported without an OAuth attempt (no browser)."""
    from aimu import aio

    attempts = []

    async def fake_connect(*, url=None, auth=None, **kw):
        attempts.append(auth)
        raise RuntimeError("Connection refused")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")

    msg = await add_mcp(url="https://down/mcp")
    assert attempts == [None]  # did not escalate to OAuth
    assert "Failed to connect" in msg


async def test_runtime_added_server_persists_and_reconnects(tmp_path, monkeypatch):
    """A server added at runtime is recorded and reconnected on the next start (the reported bug)."""
    from aimu import aio

    from kokua import mcp_registry

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    await add_mcp(url="https://svc/mcp")
    a1._store.close()
    # Recorded for reconnect, no secret on disk, auth_mode "none".
    assert mcp_registry.load(cfg.mcp_servers_path) == [{"url": "https://svc/mcp", "auth_mode": "none"}]

    # Simulate a restart: a fresh Assistant reconnects from the registry without re-adding.
    a2 = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_search" in {fn.__name__ for fn in a2._agent.tools}
    assert [conn.url for conn in a2._mcp_servers] == ["https://svc/mcp"]


async def test_oauth_server_persists_and_reconnects_with_provider(tmp_path, monkeypatch):
    """An OAuth server is recorded as auth_mode 'oauth' and reconnects via the provider directly."""
    from aimu import aio

    from kokua import mcp_registry
    from kokua.mcp_auth import ChatOAuth

    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:  # unauthenticated attempt -> challenge
            raise RuntimeError("Client error '401 Unauthorized'")
        return _FakeMCP([_fake_mcp_tool("remote_trade")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    await add_mcp(url="https://svc/mcp")
    a1._store.close()
    assert mcp_registry.load(cfg.mcp_servers_path) == [{"url": "https://svc/mcp", "auth_mode": "oauth"}]

    # Restart: reconnect goes straight to the OAuth provider (no plain attempt first).
    seen = []

    async def fake_connect2(*, url=None, auth=None, **kw):
        seen.append(auth)
        return _FakeMCP([_fake_mcp_tool("remote_trade")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect2)
    a2 = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_trade" in {fn.__name__ for fn in a2._agent.tools}
    assert len(seen) == 1 and isinstance(seen[0], ChatOAuth)  # reconnected via the provider, no re-auth dance


async def test_bearer_server_not_persisted(tmp_path, monkeypatch):
    """A bearer-token server is session-only: its secret is never written, so it is not reconnected."""
    from aimu import aio

    from kokua import mcp_registry

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_trade")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    msg = await add_mcp(url="https://svc/mcp", bearer_token="secret")
    a1._store.close()
    assert "session only" in msg
    assert mcp_registry.load(cfg.mcp_servers_path) == []

    a2 = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_trade" not in {fn.__name__ for fn in a2._agent.tools}


async def test_remove_mcp_server_drops_tools_and_forgets(tmp_path, monkeypatch):
    """remove_mcp_server removes the live tools and the persisted record, so no reconnect on restart."""
    from aimu import aio

    from kokua import mcp_registry

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    remove_mcp = next(t for t in a1._agent.tools if t.__name__ == "remove_mcp_server")
    await add_mcp(url="https://svc/mcp")

    assert await remove_mcp(url="https://nope/mcp") == "No MCP server is connected at 'https://nope/mcp'."

    msg = await remove_mcp(url="https://svc/mcp")
    assert "Disconnected" in msg and "remote_search" in msg
    assert "remote_search" not in {fn.__name__ for fn in a1._agent.tools}
    assert a1._mcp_servers == []
    assert mcp_registry.load(cfg.mcp_servers_path) == []
    a1._store.close()

    # Restart: the removed server is not reconnected.
    a2 = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_search" not in {fn.__name__ for fn in a2._agent.tools}


async def test_runtime_added_tool_is_live_in_the_same_turn(tmp_path, monkeypatch):
    """A server added mid-turn is callable that same turn (its tools join the live dispatch list).

    Regression: _prepare_run snapshots model_client.tools at run start, so appending only to
    agent.tools left the new tools out of this turn's dispatch table -> "tool not found" when the
    model called one after add_mcp_server reported it available.
    """
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("get_portfolio")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    agent = assistant._agent

    # Simulate a turn in progress: _prepare_run snapshots the live dispatch list (no remote server yet).
    agent._prepare_run()
    assert "get_portfolio" not in {fn.__name__ for fn in agent.model_client.tools}

    add_mcp = next(t for t in agent.tools if t.__name__ == "add_mcp_server")
    await add_mcp(url="https://svc/mcp")

    # Callable now, same turn: present in the live dispatch list, not just the configured agent.tools.
    assert "get_portfolio" in {fn.__name__ for fn in agent.model_client.tools}

    # And remove_mcp_server drops it from the live list too (not just agent.tools).
    remove_mcp = next(t for t in agent.tools if t.__name__ == "remove_mcp_server")
    await remove_mcp(url="https://svc/mcp")
    assert "get_portfolio" not in {fn.__name__ for fn in agent.model_client.tools}


# --- Settings (generation kwargs, display prefs, model) --------------------------------------


async def test_boot_applies_stored_settings(tmp_path):
    cfg = _config(tmp_path)
    runtime_settings.save(
        cfg.runtime_settings_path,
        {"generate_kwargs": {"temperature": 0.4, "max_tokens": 500}, "show_tools": False},
    )
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    assert client.default_generate_kwargs == {"temperature": 0.4, "max_tokens": 500}
    assert assistant._config.show_tools is False


async def test_boot_layers_runtime_over_config_generation(tmp_path):
    # config.toml [generation] is the baseline; the runtime store overrides only the keys it sets.
    cfg = _config(tmp_path, generation={"temperature": 0.1, "max_tokens": 100})
    runtime_settings.save(cfg.runtime_settings_path, {"generate_kwargs": {"temperature": 0.9}})
    client = MockAsyncModelClient([])
    await Assistant.create(cfg, FakeChannel(), client=client)
    assert client.default_generate_kwargs == {"temperature": 0.9, "max_tokens": 100}


async def test_boot_without_settings_file_writes_nothing(tmp_path):
    cfg = _config(tmp_path)
    await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    assert not cfg.runtime_settings_path.exists()


async def test_apply_settings_updates_and_persists(tmp_path):
    cfg = _config(tmp_path)
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    await assistant.apply_settings({"generate_kwargs": {"temperature": 0.5}, "show_tools": False})
    assert client.default_generate_kwargs["temperature"] == 0.5
    assert assistant._config.show_tools is False
    saved = runtime_settings.load(cfg.runtime_settings_path)
    assert saved["generate_kwargs"]["temperature"] == 0.5
    assert saved["show_tools"] is False


async def test_apply_settings_blank_field_reverts_to_config_generation(tmp_path):
    cfg = _config(tmp_path, generation={"temperature": 0.2})
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    await assistant.apply_settings({"generate_kwargs": {"temperature": 0.9}})
    assert client.default_generate_kwargs["temperature"] == 0.9
    await assistant.apply_settings({"generate_kwargs": {}})  # cleared -> back to the config baseline
    assert client.default_generate_kwargs["temperature"] == 0.2


async def test_apply_settings_switches_model(tmp_path, monkeypatch):
    first = MockAsyncModelClient(["hi"])
    assistant = await Assistant.create(_config(tmp_path, model="m1"), FakeChannel(), client=first)
    await assistant._handle(ChannelMessage(text="hello", channel="fake"))  # populate conversation state

    second = MockAsyncModelClient([])
    monkeypatch.setattr("kokua.assistant.aio.client", lambda *a, **k: second)

    await assistant.apply_settings({"model": "m2", "generate_kwargs": {}})

    assert assistant._agent.model_client is second
    assert assistant._config.model == "m2"
    # conversation restored onto the new client (system message stripped, the user turn preserved)
    assert any(m.get("content") == "hello" for m in second.messages)


async def test_current_settings_reports_effective(tmp_path):
    client = MockAsyncModelClient([])
    assistant = await Assistant.create(_config(tmp_path, model="m1"), FakeChannel(), client=client)
    await assistant.apply_settings({"generate_kwargs": {"temperature": 0.7}})
    s = assistant.current_settings()
    assert s["model"] == "m1"
    assert s["generate_kwargs"]["temperature"] == 0.7
    assert "show_thinking" in s and "show_tools" in s
