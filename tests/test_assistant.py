"""Mock-only tests for the Kokua assistant core and CLI parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import MagicMock

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


async def test_subagent_tool_is_typed_with_default_roles(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    spawn = next(t for t in assistant._agent.tools if t.__name__ == "spawn_subagent")
    # Typed mode takes (agent_type, task); the docstring lists the default roles.
    import inspect

    params = list(inspect.signature(spawn).parameters)
    assert params[:2] == ["agent_type", "task"]
    assert "researcher" in spawn.__doc__ and "coder" in spawn.__doc__


def test_build_subagent_agent_types_clamps_to_enabled_groups(tmp_path):
    from kokua.build import _build_subagent_agent_types

    # coder wants fs+compute; only web enabled globally -> coder ends up with no tools.
    cfg = _config(tmp_path, tools=["web"])
    types = _build_subagent_agent_types(cfg)
    assert types["coder"]["tools"] == []
    researcher_names = {fn.__name__ for fn in types["researcher"]["tools"]}
    assert "web_search" in researcher_names  # web group survived
    # The description is the first line of the built system_message (AIMU's menu line).
    assert types["researcher"]["system_message"].splitlines()[0] == (
        "Research specialist: gather and verify information from the web."
    )


def test_subagent_roles_nonempty_when_tools_all(tmp_path):
    from kokua.build import _build_subagent_agent_types

    cfg = _config(tmp_path, tools=["all"])
    types = _build_subagent_agent_types(cfg)
    assert types["coder"]["tools"]  # non-empty: fs+compute groups now enabled
    assert any(fn.__name__ == "execute_python" for fn in types["coder"]["tools"])
    assert types["generalist"]["tools"]  # non-empty: all groups enabled


async def test_subagent_tool_routes_approval_to_parent(tmp_path, monkeypatch):
    import kokua.build as build_mod

    captured = {}

    def fake_make_async_subagent_tool(model, *, agent_types, tool_approval, **kwargs):
        captured["tool_approval"] = tool_approval

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """researcher: research. coder: code."""
            return "ok"

        spawn_subagent.__name__ = "spawn_subagent"
        # AIMU's tool machinery inspects these attributes; the fake must carry them to survive Assistant.create.
        spawn_subagent.__tool_is_async__ = True
        spawn_subagent.__tool_is_streaming__ = False
        spawn_subagent.__tool_spec__ = {"function": {"name": "spawn_subagent"}}
        return spawn_subagent

    monkeypatch.setattr(build_mod, "make_async_subagent_tool", fake_make_async_subagent_tool)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert captured["tool_approval"] == assistant._approve


async def test_subagent_concurrent_flag_reaches_agent(tmp_path):
    on = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert on._agent.concurrent_tool_calls is True
    off = await Assistant.create(
        _config(tmp_path, subagents_concurrent=False), FakeChannel(), client=MockAsyncModelClient([])
    )
    assert off._agent.concurrent_tool_calls is False


async def test_assistant_unknown_tool_group_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown tool group"):
        await Assistant.create(_config(tmp_path, tools=["bogus"]), FakeChannel(), client=MockAsyncModelClient([]))


async def test_assistant_handles_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Sure, done."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(ChannelMessage(text="do a thing", channel="fake"), conversation_id=assistant._active_id)

    assert channel.sent == ["Sure, done."]
    assert assistant.history  # persisted at least the turn


async def test_assistant_proactive_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Don't forget lunch."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._proactive("remind")

    assert channel.sent == ["Don't forget lunch."]


async def test_assistant_proactive_tags_turn_provenance(tmp_path):
    from aimu.models import PROVENANCE_KEY, PROVENANCE_PROACTIVE

    channel = FakeChannel()
    client = MockAsyncModelClient(["Time for a walk."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._proactive("remind")

    tagged = [m.get(PROVENANCE_KEY) for m in assistant._agent.model_client.messages]
    assert PROVENANCE_PROACTIVE in tagged
    assert all(p in (None, PROVENANCE_PROACTIVE) for p in tagged)


async def test_assistant_persists_and_restores(tmp_path):
    cfg = _config(tmp_path)

    channel1 = FakeChannel()
    client1 = MockAsyncModelClient(["first reply"])
    assistant1 = await Assistant.create(cfg, channel1, client=client1)
    await assistant1._handle(ChannelMessage(text="remember this"), conversation_id=assistant1._active_id)
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

    await assistant._handle(
        ChannelMessage(text="plan my trip to Kauai", channel="fake"), conversation_id=assistant._active_id
    )

    stored = assistant._store.get(assistant._session.key)
    assert any(m.get("content") == "plan my trip to Kauai" for m in stored.messages)
    assert stored.metadata["title"] == "plan my trip to Kauai"


async def test_history_returns_active_session_messages(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))
    await assistant._handle(ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id)
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

    await assistant._handle(ChannelMessage(text="hello there", channel="fake"), conversation_id=assistant._active_id)
    assert len(channel.conversation_pushes) == 1  # title just set -> one push
    assert channel.conversation_pushes[0][0]["title"] == "hello there"

    await assistant._handle(ChannelMessage(text="again", channel="fake"), conversation_id=assistant._active_id)
    assert len(channel.conversation_pushes) == 1  # title already set -> no further push


async def test_list_conversations_recency_desc(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="first chat", channel="fake"), conversation_id=assistant._active_id)
    first_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="second chat", channel="fake"), conversation_id=assistant._active_id)
    second_id = assistant._session.key

    items = assistant.list_conversations()
    assert [i["id"] for i in items] == [second_id, first_id]  # most recent first
    assert items[0]["title"] == "second chat"
    assert items[0]["active"] is True and items[1]["active"] is False


async def test_new_conversation_resets_agent(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="old chat", channel="fake"), conversation_id=assistant._active_id)
    assert assistant._agent.model_client.messages  # has the old turn

    new_id = await assistant.new_conversation()
    assert assistant._session.key == new_id
    assert assistant._session.messages == []
    assert assistant._agent.model_client.messages == []  # the new conversation's agent is freshly built


async def test_select_conversation_restores_messages(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="keep me", channel="fake"), conversation_id=assistant._active_id)
    first_id = assistant._session.key
    await assistant.new_conversation()
    assert not any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)

    await assistant.select_conversation(first_id)
    assert assistant._session.key == first_id
    assert any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)


async def test_delete_inactive_conversation_leaves_active(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="old chat", channel="fake"), conversation_id=assistant._active_id)
    old_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="current chat", channel="fake"), conversation_id=assistant._active_id)
    active_id = assistant._session.key

    await assistant.delete_conversation(old_id)
    assert old_id not in assistant._store.list_keys()
    assert assistant._session.key == active_id  # active unchanged
    assert any(m.get("content") == "current chat" for m in assistant._agent.model_client.messages)


async def test_delete_active_switches_to_most_recent_remaining(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="keep me", channel="fake"), conversation_id=assistant._active_id)
    keep_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="delete me", channel="fake"), conversation_id=assistant._active_id)
    delete_id = assistant._session.key

    await assistant.delete_conversation(delete_id)
    assert delete_id not in assistant._store.list_keys()
    assert assistant._session.key == keep_id  # switched to the remaining one
    assert any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)


async def test_delete_last_conversation_creates_fresh_empty(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="only chat", channel="fake"), conversation_id=assistant._active_id)
    only_id = assistant._session.key

    await assistant.delete_conversation(only_id)
    assert only_id not in assistant._store.list_keys()
    assert assistant._session.key != only_id  # a fresh, empty active conversation
    assert assistant._session.messages == []
    assert assistant._agent.model_client.messages == []


async def test_registry_used_for_active_conversation(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    agent = assistant._registry.get(assistant._active_id)
    assert assistant._agent is agent  # the _agent property resolves to the active conversation's agent


async def test_switch_conversation_isolates_message_lists(tmp_path):
    cfg = _config(tmp_path)
    factory = lambda cid: MockAsyncModelClient(["reply in c1"])  # noqa: E731
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=factory)
    first_id = assistant._active_id
    # Run a turn in the first conversation.
    await assistant._handle(ChannelMessage(text="hello", sender="t", channel="t"), conversation_id=assistant._active_id)
    second_id = await assistant.new_conversation()
    assert second_id != first_id
    # The new conversation's agent has its own (empty) message list.
    assert assistant._agent.model_client.messages == []
    # Switching back does not replay onto the wrong agent.
    await assistant.select_conversation(first_id)
    assert any("reply in c1" == m.get("content") for m in assistant._agent.model_client.messages)


async def test_handle_persists_to_its_own_conversation_not_active(tmp_path):
    # A turn bound to conversation A must persist to A even if _active_id moved to B mid-flight.
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["reply-A"]))
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()  # _active_id now B
    assert assistant._active_id == conv_b
    await assistant._handle(ChannelMessage(text="hi", sender="t", channel="t"), conversation_id=conv_a)
    stored_a = assistant._store.get(conv_a)
    assert any(m.get("content") == "reply-A" for m in stored_a.messages)
    stored_b = assistant._store.get(conv_b)
    assert not any(m.get("content") == "reply-A" for m in stored_b.messages)


async def test_select_conversation_reverts_active_id_on_build_failure(tmp_path):
    from kokua.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial conversation builds fine; the selected one fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    original_id = assistant._active_id

    with pytest.raises(ModelClientError):
        await assistant.select_conversation("does-not-exist-yet")

    # The failed build must not leave the assistant pointed at an unbuildable conversation.
    assert assistant._active_id == original_id


async def test_new_conversation_reverts_active_id_on_build_failure(tmp_path):
    from kokua.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial conversation builds fine; the new one fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    original_id = assistant._active_id

    with pytest.raises(ModelClientError):
        await assistant.new_conversation()

    # The failed build must not leave the assistant pointed at the unbuildable new conversation;
    # it reverts to the one that was active before the call (its session record still lingers in
    # the store, unused but harmless).
    assert assistant._active_id == original_id


async def test_delete_conversation_reverts_active_id_to_deleted_id_on_build_failure(tmp_path):
    """delete_conversation's revert is documented as best-effort: the deleted conversation's store
    record and registry entry are already gone by the time the replacement's build fails, so
    reverting only restores the id, not a working conversation. This asserts that documented
    behavior (not a full rollback, which would need deferring the delete itself)."""
    from kokua.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial (soon-to-be-deleted) conversation builds fine; its
            # replacement (a fresh empty conversation, since none remain) fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    original_id = assistant._active_id

    with pytest.raises(ModelClientError):
        await assistant.delete_conversation(original_id)

    # Reverts to the just-deleted id (documented best-effort semantics), not to some other,
    # never-vetted conversation.
    assert assistant._active_id == original_id
    # The delete itself was not rolled back: the store no longer has a record for that id
    # (TinyDBSessionStore.get returns a fresh, unsaved Session for a missing key, not None).
    assert original_id not in assistant._store.list_keys()


async def test_persist_writes_active_conversation(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(
        cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["hi there"])
    )
    active = assistant._active_id
    await assistant._handle(ChannelMessage(text="hello", sender="t", channel="t"), conversation_id=assistant._active_id)
    reloaded = assistant._store.get(active)
    assert any(m.get("content") == "hi there" for m in reloaded.messages)


async def test_model_switch_applies_to_all_live_agents(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    second = await assistant.new_conversation()  # noqa: F841
    await assistant.select_conversation(first)

    built = []

    def fake_client(model, system=None):
        c = MockAsyncModelClient([])
        c.model = MagicMock(supports_tools=True, supports_thinking=False, supports_vision=False)
        built.append(model)
        return c

    monkeypatch.setattr("kokua.assistant.aio.client", fake_client)
    await assistant._switch_model("anthropic:claude-x")
    # Both cached agents got a rebuilt client for the new model.
    assert built.count("anthropic:claude-x") == len(assistant._registry.live_agents())


async def test_new_conversation_agent_carries_layered_generate_kwargs(tmp_path):
    # A lazily-built conversation's client must carry the same effective generation kwargs the active
    # agent has (provider defaults < config.generation < runtime override), not bare provider defaults.
    cfg = _config(tmp_path, generation={"temperature": 0.2})
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    new_id = await assistant.new_conversation()
    new_agent = assistant._registry.get(new_id)
    assert new_agent.model_client.default_generate_kwargs.get("temperature") == 0.2
    assert assistant.current_settings()["generate_kwargs"].get("temperature") == 0.2


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
    from kokua.channels.web import streaming_conversation

    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    # Foreground: the calling turn's conversation is the one currently viewed.
    token = streaming_conversation.set(assistant._active_id)
    try:
        task = asyncio.create_task(assistant._approve("add_skill_script", {"skill_name": "x"}))
        await asyncio.sleep(0)  # let the policy register the pending approval and prompt
        assert assistant._pending_approval is not None
        assert channel.sent  # a prompt was sent to the user
        assistant._pending_approval.set_result(True)
        assert await task is True
    finally:
        streaming_conversation.reset(token)


async def test_approve_proactive_auto_denies_gated_tool(tmp_path):
    from kokua.channels.web import streaming_conversation

    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    # Background: the calling turn's conversation ("elsewhere") isn't the one being viewed.
    token = streaming_conversation.set("elsewhere")
    try:
        assert await assistant._approve("add_skill_script", {}) is False
        assert channel.sent == []  # auto-deny: no prompt, no waiting
    finally:
        streaming_conversation.reset(token)


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
    assert assistant._tracker.get(assistant._active_id) is None  # the answer did not start a new turn


class _RequestsToolOnce(MockAsyncModelClient):
    """Requests one gated tool call on the first turn (single-turn: the Agent's engine dispatches it),
    then answers plainly. Lets a real run exercise the approval gate instead of the mock's faked round."""

    def __init__(self, name: str, arguments: dict):
        super().__init__([])
        self._name = name
        self._arguments = arguments
        self._requested = False

    async def _chat(
        self, user_message=None, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None
    ):
        if user_message is not None:
            self.messages.append({"role": "user", "content": user_message})
        if not self._requested:
            self._requested = True
            self.messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"type": "function", "function": {"name": self._name, "arguments": self._arguments}, "id": "x"}
                    ],
                }
            )
            return ""
        self.messages.append({"role": "assistant", "content": "ok"})
        return "ok"


async def test_denied_gated_tool_does_not_run(tmp_path):
    cfg = _config(tmp_path, confirm_tools=["add_skill_script"])
    client = _RequestsToolOnce("add_skill_script", {"skill_name": "disk", "filename": "u.py", "content": "print(1)\n"})
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    # No streaming_conversation is set around this call, so it defaults to None -- not the viewed
    # conversation -- making _approve auto-deny without an interactive prompt. That exercises the real
    # dispatch path (the Agent's tool-loop engine + approval gate) via a normal run.

    await assistant._agent.run("go")

    denied = [m for m in client.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'add_skill_script' was not approved."
    assert not (cfg.skills_dir / "disk" / "scripts" / "u.py").exists()


async def test_approve_serializes_concurrent_gated_calls(tmp_path):
    """Two concurrent gated approvals must not clobber each other's pending future.

    Without the lock the interleaved coroutines both call asyncio.gather concurrently. The first
    call creates self._pending_approval and yields at the sleep; the second then overwrites it with
    a fresh future before the first has resolved. The first call then calls set_result on the
    already-cleared (None) reference, raising AttributeError ('NoneType' has no attribute
    'set_result'). With the lock the second call waits until the first has fully completed (future
    resolved, pending_approval cleared) before it acquires the lock, creates its own future, and
    resolves it safely.
    """
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    prompts: list[str] = []
    order: list[str] = []

    async def fake_prompt(name, arguments):
        prompts.append(name)
        # Yield to the event loop before resolving so the two gathered coroutines can interleave.
        # Without the lock the second call overwrites self._pending_approval here, causing the
        # first call to resolve the wrong future and the second to deadlock (or raise
        # InvalidStateError if its future is resolved twice).
        await asyncio.sleep(0)
        assistant._pending_approval.set_result(True)

    assistant._prompt_approval = fake_prompt

    async def call(tag):
        result = await assistant._approve("execute_python", {"code": tag})
        order.append(tag)
        return result

    # Foreground: both concurrent calls belong to the viewed conversation.
    token = streaming_conversation.set(assistant._active_id)
    try:
        results = await asyncio.wait_for(asyncio.gather(call("a"), call("b")), timeout=2.0)
    finally:
        streaming_conversation.reset(token)

    assert results == [True, True]
    assert prompts == ["execute_python", "execute_python"]  # both prompted, one at a time
    assert set(order) == {"a", "b"}


# --- foreground-gated approval + switch-away concurrency (Task 6) -----------------------------


async def test_background_turn_auto_denies_gated_tool(tmp_path):
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    viewed = assistant._active_id
    await assistant.new_conversation()  # _active_id now the new (background) conversation
    background = assistant._active_id
    await assistant.select_conversation(viewed)  # make `viewed` active again
    # A turn running in `background` is not the viewed conversation -> auto-deny.
    token = streaming_conversation.set(background)
    try:
        assert await assistant._approve("execute_python", {}) is False
    finally:
        streaming_conversation.reset(token)


async def test_foreground_turn_prompts_for_approval(tmp_path):
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    channel = FakeChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    viewed = assistant._active_id
    token = streaming_conversation.set(viewed)
    try:
        approve_task = asyncio.create_task(assistant._approve("execute_python", {}))
        await asyncio.sleep(0.01)
        assert assistant._pending_approval is not None and not assistant._pending_approval.done()
        assistant._pending_approval.set_result(True)
        assert await approve_task is True
    finally:
        streaming_conversation.reset(token)


async def test_switch_away_does_not_cancel_running_turn(tmp_path):
    from kokua.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    conv_a = assistant._active_id

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(conv_a, TurnInfo(handle=handle, started=0.0, preview="p"))
    await assistant.new_conversation()  # switches away from A
    assert not handle.done  # A's turn keeps running
    handle.cancel()
    await asyncio.gather(handle.task, return_exceptions=True)


async def test_select_conversation_does_not_cancel_running_turn(tmp_path):
    """Mirrors the new_conversation case: select_conversation must not cancel the turn either."""
    from kokua.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()
    await assistant.select_conversation(conv_a)

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(conv_a, TurnInfo(handle=handle, started=0.0, preview="p"))
    await assistant.select_conversation(conv_b)  # switches away from A
    assert not handle.done
    handle.cancel()
    await asyncio.gather(handle.task, return_exceptions=True)


async def test_switch_away_resolves_pending_approval_as_denied(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    assistant._pending_approval = asyncio.get_running_loop().create_future()
    await assistant.new_conversation()  # switching away
    assert assistant._pending_approval is None or assistant._pending_approval.result() is False


async def test_switch_away_resolves_pending_plan_as_rejected(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    assistant._pending_plan = asyncio.get_running_loop().create_future()
    await assistant.select_conversation(assistant._active_id)  # switching (even to the same id)
    assert assistant._pending_plan is None or assistant._pending_plan.result() is None


async def test_delete_conversation_cancels_its_own_running_turn(tmp_path):
    """delete_conversation cancels the DELETED conversation's own turn -- there is no conversation
    left for it to keep persisting to -- even if that conversation isn't the one being viewed."""
    from kokua.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()  # active_id now B; A is inactive
    assert assistant._active_id == conv_b

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(conv_a, TurnInfo(handle=handle, started=0.0, preview="p"))

    await assistant.delete_conversation(conv_a)
    await asyncio.sleep(0.01)
    assert handle.done  # A's own turn was cancelled
    assert assistant._active_id == conv_b  # deleting an inactive conversation leaves active untouched


async def test_switch_methods_sync_channel_active_conversation_id(tmp_path):
    """Item 1 from the Task 5 review: the muting key (WebChannel.active_conversation_id) and the
    background-completion notification key (Assistant._active_id) must agree on what's viewed, so
    every switch method mirrors _active_id onto the channel (when it tracks one)."""

    class _TrackingChannel(FakeChannel):
        def __init__(self):
            super().__init__()
            self.active_conversation_id = None

    channel = _TrackingChannel()
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))

    new_id = await assistant.new_conversation()
    assert channel.active_conversation_id == new_id == assistant._active_id

    other_id = await assistant.new_conversation()
    assert channel.active_conversation_id == other_id == assistant._active_id

    await assistant.select_conversation(new_id)
    assert channel.active_conversation_id == new_id == assistant._active_id

    await assistant.delete_conversation(other_id)
    assert channel.active_conversation_id == assistant._active_id  # unaffected: not the deleted one

    await assistant.delete_conversation(new_id)  # deletes the viewed one -> falls back to a fresh one
    assert channel.active_conversation_id == assistant._active_id


async def test_background_turn_notifies_on_success_when_switched_away(tmp_path):
    """Reconciled notification key (item 1): fires exactly when the turn's conversation was muted,
    i.e. `conversation_id != self._active_id` at completion -- the same notion WebChannel's own
    muting uses once its active_conversation_id is kept in sync (see the sync test above)."""

    class _NotifyChannel(FakeChannel):
        async def send_notification(self, text: str) -> None:
            self.sent.append(f"[notify] {text}")

    channel = _NotifyChannel()
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient(["ok"]))
    conv_a = assistant._active_id
    await assistant.new_conversation()  # switch away from A before A's (background) turn runs
    assert assistant._active_id != conv_a

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=conv_a)

    assert any(s.startswith("[notify]") for s in channel.sent)


async def test_background_turn_error_does_not_notify(tmp_path):
    """Item 2 from the Task 5 review: a muted turn that errors must not claim 'reply ready'."""

    class _NotifyChannel(FakeChannel):
        async def send_notification(self, text: str) -> None:
            self.sent.append(f"[notify] {text}")

    channel = _NotifyChannel()
    cfg = _config(tmp_path)
    assistant = await Assistant.create(
        cfg, channel, client_factory=lambda cid: MockAsyncModelClient([Exception("boom")])
    )
    conv_a = assistant._active_id
    await assistant.new_conversation()  # switch away from A before A's (background) turn runs
    assert assistant._active_id != conv_a

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=conv_a)

    assert not any(s.startswith("[notify]") for s in channel.sent)
    assert any("failed" in s for s in channel.sent)  # the error reply itself still went out (muted)


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
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:  # let the cancelled turn finish its (stopped) + persist
        await asyncio.gather(info.handle.task, return_exceptions=True)

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
    assert assistant._tracker.get(assistant._active_id) is None


async def test_two_conversations_turns_run_concurrently(tmp_path):
    """A direct TurnGate exercise: two different conversations' turns overlap, proving the gate no
    longer serializes across conversations the way the old global lock did."""
    cfg = _config(tmp_path)
    gate_events: list[str] = []

    def factory(cid):
        return MockAsyncModelClient(["done"])

    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=factory)
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()

    async def run_turn(cid):
        async with assistant._gate.turn(cid):
            gate_events.append(f"in-{cid}")
            await asyncio.sleep(0.02)
            gate_events.append(f"out-{cid}")

    await asyncio.gather(run_turn(conv_a), run_turn(conv_b))
    # Both entered before either exited -> concurrent.
    assert gate_events.index(f"in-{conv_b}") < gate_events.index(f"out-{conv_a}")


async def test_stop_cancels_active_conversation_turn(tmp_path):
    """/stop's helper cancels the tracked turn for the viewed conversation directly (no serve loop
    needed to exercise it)."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    from kokua.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(assistant._active_id, TurnInfo(handle=handle, started=0.0, preview="p"))
    assistant._stop_active_turn()  # helper the /stop branch calls
    await asyncio.sleep(0.01)
    assert handle.done


# --- /diag command + logging ------------------------------------------------------------------


def test_logs_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path, memory=False)
    assert cfg.logs_path == tmp_path / "logs"


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


def test_configure_logging_writes_to_log_file(tmp_path):
    import logging as _logging
    from logging.handlers import RotatingFileHandler

    from kokua.logging_setup import configure_logging

    cfg = _config(tmp_path)
    try:
        configure_logging(cfg)
        _logging.getLogger("kokua").info("hello-diag-test-line")
        logfile = cfg.logs_path / "kokua.log"
        assert logfile.exists()
        assert "hello-diag-test-line" in logfile.read_text()
    finally:
        for name in ("kokua", "aimu"):
            lg = _logging.getLogger(name)
            for h in list(lg.handlers):
                if isinstance(h, RotatingFileHandler):
                    lg.removeHandler(h)
                    h.close()


def test_configure_logging_is_idempotent(tmp_path):
    import logging as _logging
    from logging.handlers import RotatingFileHandler

    from kokua.logging_setup import configure_logging

    cfg = _config(tmp_path)
    try:
        configure_logging(cfg)
        configure_logging(cfg)
        handlers = [h for h in _logging.getLogger("kokua").handlers if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
    finally:
        for name in ("kokua", "aimu"):
            lg = _logging.getLogger(name)
            for h in list(lg.handlers):
                if isinstance(h, RotatingFileHandler):
                    lg.removeHandler(h)
                    h.close()


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
    # reload_skills() re-snapshotted the skill tools; the tool-loop engine reads them via
    # _effective_tools each round, so the new script tool is dispatchable on the next run.
    assert "disk__usage" in [fn.__name__ for fn in assistant._agent._effective_tools()]


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
    """A server added mid-turn is callable that same turn.

    The tool-loop engine re-reads the agent's effective tools each round, so a tool appended to
    agent.tools by add_mcp_server joins the dispatch table immediately (and remove_mcp_server drops it),
    without the assistant having to touch the model client.
    """
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("get_portfolio")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    agent = assistant._agent

    assert "get_portfolio" not in {fn.__name__ for fn in agent._effective_tools()}

    add_mcp = next(t for t in agent.tools if t.__name__ == "add_mcp_server")
    await add_mcp(url="https://svc/mcp")

    # Callable now, same turn: the engine's per-round effective-tools read includes it.
    assert "get_portfolio" in {fn.__name__ for fn in agent._effective_tools()}

    remove_mcp = next(t for t in agent.tools if t.__name__ == "remove_mcp_server")
    await remove_mcp(url="https://svc/mcp")
    assert "get_portfolio" not in {fn.__name__ for fn in agent._effective_tools()}


async def _await_value(value):
    return value


async def test_add_mcp_server_fans_out_to_all_live_agents(tmp_path, monkeypatch):
    """Adding a server at runtime lands its tools on every live agent, not just the active one."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    await assistant.new_conversation()
    await assistant.select_conversation(first)

    monkeypatch.setattr(
        "kokua.mcp.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("remote_ping")]), "none"))
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_tool("https://example/mcp")

    live = assistant._registry.live_agents()
    assert len(live) == 2
    for agent in live:
        assert any(getattr(t, "__name__", "") == "remote_ping" for t in agent.tools)


async def test_remove_mcp_server_fans_out_to_all_live_agents(tmp_path, monkeypatch):
    """Removing a server drops its tools from every live agent."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    await assistant.new_conversation()
    await assistant.select_conversation(first)

    monkeypatch.setattr(
        "kokua.mcp.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("remote_ping")]), "none"))
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_tool("https://example/mcp")
    remove_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    await remove_tool("https://example/mcp")

    for agent in assistant._registry.live_agents():
        assert not any(getattr(t, "__name__", "") == "remote_ping" for t in agent.tools)


async def test_remove_mcp_server_keeps_tool_still_owned_by_another_server(tmp_path, monkeypatch):
    """Removing a server must not strip a tool name still owned by another live connection.

    Two servers both expose a tool named "shared_tool". Attach dedups by __name__, so the second
    server's as_tools() call adds nothing new to agent.tools, but ServerConnection.tools still
    records "shared_tool" for both connections (it stores all of a server's tool names, not just
    the ones it uniquely added). Removing the second server must not strip the tool, because the
    first server still owns and exposes it.
    """
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    monkeypatch.setattr(
        "kokua.mcp.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("shared_tool")]), "none"))
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_tool(url="https://server-a/mcp")
    await add_tool(url="https://server-b/mcp")

    remove_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    msg = await remove_tool(url="https://server-b/mcp")

    assert "shared_tool" in {fn.__name__ for fn in assistant._agent.tools}
    assert "shared_tool" not in msg


async def test_newly_built_agent_gets_already_connected_server(tmp_path, monkeypatch):
    """A conversation whose agent is built after a server was added still gets that server's tools."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))

    monkeypatch.setattr(
        "kokua.mcp.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("remote_ping")]), "none"))
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_tool("https://example/mcp")

    new_id = await assistant.new_conversation()
    new_agent = assistant._registry.get(new_id)
    names = [fn.__name__ for fn in new_agent.tools]
    assert names.count("remote_ping") == 1


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
    await assistant._handle(
        ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id
    )  # populate conversation state

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


async def test_create_wraps_unbuildable_client_as_model_client_error(tmp_path, monkeypatch):
    import kokua.assistant as assistant_mod
    from kokua.assistant import ModelClientError

    def boom(*args, **kwargs):
        raise ValueError("No model specified and no default could be resolved.")

    monkeypatch.setattr(assistant_mod.aio, "client", boom)
    with pytest.raises(ModelClientError, match="no default could be resolved"):
        await Assistant.create(_config(tmp_path), FakeChannel())


async def test_proactive_new_session_runs_in_fresh_conversation(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["task output"])
    )
    # Establish an active conversation with one real turn.
    await assistant._handle(ChannelMessage(text="hello there", channel="fake"), conversation_id=assistant._active_id)
    active_key = assistant._session.key
    active_len = len(assistant._session.messages)

    await assistant._proactive("run the report", new_session=True, task_name="report")

    # Active conversation is restored and untouched.
    assert assistant._session.key == active_key
    assert len(assistant._session.messages) == active_len
    # A new conversation exists, titled from the task, holding the task's turn.
    keys = assistant._store.list_keys()
    assert len(keys) == 2
    new_key = next(k for k in keys if k != active_key)
    new_session = assistant._store.get(new_key)
    assert new_session.metadata["title"] == "report"
    assert any(m.get("content") == "task output" for m in new_session.messages)
    # Sidebar refreshed and a notice was sent.
    assert channel.conversation_pushes
    assert any("report" in s for s in channel.sent)


async def test_proactive_new_session_degrades_on_single_conversation_channel(tmp_path):
    channel = FakeChannel()  # no send_conversations
    client = MockAsyncModelClient(["task output"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    active_key = assistant._session.key

    await assistant._proactive("run the report", new_session=True, task_name="report")

    # No extra conversation; ran in place and pushed the reply.
    assert assistant._store.list_keys() == [active_key]
    assert channel.sent == ["task output"]


async def test_proactive_new_session_holds_at_most_one_gate_turn(tmp_path):
    """Regression for a nested-gate-hold deadlock: _proactive's new-session branch must not keep an
    outer gate.turn(self._active_id) held while _run_in_new_session takes its own inner
    gate.turn(new_id) -- two simultaneous reader holds from the same task can wedge against a
    concurrent gate.exclusive() (writer-preferring: the writer waits for readers to drain, but one of
    this task's own readers is stuck waiting on the other's lock, which never yields to the writer).
    Asserts a single hold is ever observed, and that none is left over afterward.
    """
    channel = _ConvCapturingChannel()
    observed: list[int] = []

    class _RecordingClient(MockAsyncModelClient):
        async def _chat(self, *args, **kwargs):
            observed.append(assistant._gate.active_turns())
            return await super()._chat(*args, **kwargs)

    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: _RecordingClient(["task output"])
    )

    await assistant._proactive("run the report", new_session=True, task_name="report")

    assert observed == [1]  # exactly one gate hold was active while the new session's turn ran
    assert assistant._gate.active_turns() == 0  # and none left over afterward

    # No wedged reader left behind: a concurrent exclusive() hold must complete promptly.
    async with asyncio.timeout(1.0):
        async with assistant._gate.exclusive():
            pass


async def test_proactive_new_session_auto_denies_gated_tool_and_never_hijacks_active_id(tmp_path):
    """Critical regression: _run_in_new_session must never touch self._active_id.

    Before the fix it swapped self._active_id to the new session's id for the run's duration (restored
    in a finally). Under Phase B concurrency that made _approve see streaming_conversation ==
    self._active_id (both the new session) -- i.e. foreground -- so a gated tool call inside a
    scheduled new-session run would PROMPT instead of auto-denying, and a concurrent user switch
    during the run would be silently clobbered back by the finally. Asserts both the auto-deny and
    that self._active_id (the viewed conversation) is untouched DURING the run, not just after.
    """
    from kokua.channels.web import streaming_conversation

    observed: dict[str, object] = {}

    class _RecordingRequestsToolOnce(_RequestsToolOnce):
        async def _chat(self, *args, **kwargs):
            # Captured mid-run (inside the agent's tool-dispatch call), not after -- the bug this
            # guards against only manifests while the scheduled turn is actually in flight.
            observed["active_id_during_run"] = assistant._active_id
            observed["streaming_conversation_during_run"] = streaming_conversation.get()
            return await super()._chat(*args, **kwargs)

    channel = _ConvCapturingChannel()
    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    assistant = await Assistant.create(
        cfg, channel, client_factory=lambda cid: _RecordingRequestsToolOnce("execute_python", {"code": "1+1"})
    )
    viewed = assistant._active_id

    await assistant._proactive("run the report", new_session=True, task_name="report")

    # Mid-run, the viewed conversation was never hijacked, and the run's own streaming context is a
    # *different* conversation than the viewed one -- the precondition for _approve to auto-deny.
    assert observed["active_id_during_run"] == viewed
    assert observed["streaming_conversation_during_run"] != viewed

    # The gated tool call the scheduled run made was denied, not prompted.
    new_id = next(k for k in assistant._store.list_keys() if k != viewed)
    new_session = assistant._store.get(new_id)
    denied = [m for m in new_session.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'execute_python' was not approved."

    # And self._active_id is still the viewed conversation after the run completes.
    assert assistant._active_id == viewed


async def test_create_registers_scheduling_tools(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {getattr(fn, "__name__", None) for fn in assistant._agent.tools}
    assert {"schedule_task", "list_scheduled_tasks", "cancel_scheduled_task"} <= names


async def test_create_arms_persisted_tasks_and_drops_past_once(tmp_path):
    from kokua import scheduling

    cfg = _config(tmp_path)
    scheduling.add(
        cfg.scheduled_tasks_path,
        {
            "id": "stale",
            "name": "o",
            "prompt": "p",
            "schedule": {"type": "once", "at": "2000-01-01T00:00:00"},
            "new_session": False,
            "created_at": "x",
            "enabled": True,
        },
    )
    await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    # Past-due one-shot was dropped during boot arming.
    assert scheduling.load(cfg.scheduled_tasks_path) == []


def test_cli_frontend_reports_model_client_error(tmp_path, monkeypatch, capsys):
    from kokua.assistant import ModelClientError
    from kokua.frontends import cli as cli_frontend

    async def boom(*args, **kwargs):
        raise ModelClientError("no default could be resolved; set AIMU_LANGUAGE_MODEL")

    monkeypatch.setattr(cli_frontend.Assistant, "create", boom)
    args = build_arg_parser().parse_args([])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli_frontend.run(_config(tmp_path), args))
    assert exc.value.code == 1
    assert "no default could be resolved" in capsys.readouterr().err


def test_make_agent_builder_wires_and_restores(tmp_path):
    from aimu.sessions import Session, TinyDBSessionStore
    from kokua.build import build_memory, make_agent_builder

    config = _config(tmp_path)  # existing helper
    store = TinyDBSessionStore(str(config.sessions_path))
    session = Session(
        key="c1",
        metadata={},
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    )
    store.save(session)
    _, _, memory_tools = build_memory(config)

    async def noop(*a, **k):
        return None

    build = make_agent_builder(
        config,
        client_factory=lambda cid: MockAsyncModelClient([]),
        notify=noop,
        oauth_storage_dir=config.data_dir / "mcp-oauth",
        connections=[],
        memory_tools=memory_tools,
        tool_approval=lambda name, args: True,
        scheduler_tools=[],
        store=store,
        images_path=config.images_path,
        for_each_agent=lambda apply: None,
    )
    agent = build("c1")
    assert agent.tool_approval is not None
    tool_names = {getattr(t, "__name__", None) for t in agent.tools}
    assert "author_skill" in tool_names
    # Messages for this conversation were restored onto the fresh agent's client.
    assert any(m.get("content") == "hello" for m in agent.model_client.messages)
