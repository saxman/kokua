"""Building an agent: tool groups, sub-agent roles, the lean supervisor, memory, and skills."""

from __future__ import annotations


import pytest


from kokua.config import AssistantConfig, MCPServerConfig
from kokua.core.assistant import Assistant
from tests.channels import FakeChannel, _config
from tests.fakes import _FakeMCP, _await_value, _fake_mcp_tool
from tests.helpers import MockAsyncModelClient


_MEMORY_TOOL_NAMES = {
    "store_memory",
    "search_memories",
    "list_memories",
    "save_document",
    "read_document",
    "list_documents",
    "search_documents",
}


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
    from kokua.core.build import _build_subagent_agent_types

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
    from kokua.core.build import _build_subagent_agent_types

    cfg = _config(tmp_path, tools=["all"])
    types = _build_subagent_agent_types(cfg)
    assert types["coder"]["tools"]  # non-empty: fs+compute groups now enabled
    assert any(fn.__name__ == "execute_python" for fn in types["coder"]["tools"])
    assert types["generalist"]["tools"]  # non-empty: all groups enabled


def test_worker_role_resolves_mcp_and_tool_pack_sources(tmp_path):
    from types import SimpleNamespace

    from kokua.core.build import _build_subagent_agent_types
    from kokua.config import MCPServerConfig

    def stock_quote():  # fake MCP tool callable -- the resolver only reads __name__
        pass

    def make_pdf():  # fake tool-pack tool
        pass

    cfg = _config(
        tmp_path,
        tools=["compute"],
        mcp_servers=[MCPServerConfig(url="https://broker/mcp", name="stocks")],
        subagent_roles={
            "trader": {
                "description": "Trades.",
                "groups": ["compute"],
                "mcp_servers": ["stocks"],  # matched by name
                "tool_packs": ["pdf"],
            }
        },
    )
    connections = [SimpleNamespace(url="https://broker/mcp", callables=[stock_quote])]
    by_pack = {"pdf": [make_pdf]}
    names = {fn.__name__ for fn in _build_subagent_agent_types(cfg, connections, by_pack)["trader"]["tools"]}
    assert "stock_quote" in names  # named MCP server's tool
    assert "make_pdf" in names  # tool-pack's tool
    assert "calculate" in names  # built-in compute group still included


def test_worker_role_resolves_mcp_by_raw_url(tmp_path):
    from types import SimpleNamespace

    from kokua.core.build import _build_subagent_agent_types

    def remote_tool():
        pass

    cfg = _config(tmp_path, tools=["none"], subagent_roles={"r": {"mcp_servers": ["https://raw/mcp"]}})
    connections = [SimpleNamespace(url="https://raw/mcp", callables=[remote_tool])]
    types = _build_subagent_agent_types(cfg, connections, {})
    assert [fn.__name__ for fn in types["r"]["tools"]] == ["remote_tool"]


def test_worker_role_unknown_sources_drop_silently(tmp_path):
    from kokua.core.build import _build_subagent_agent_types

    cfg = _config(
        tmp_path,
        tools=["all"],
        subagent_roles={"r": {"groups": ["web"], "mcp_servers": ["nope"], "tool_packs": ["ghost"]}},
    )
    names = {fn.__name__ for fn in _build_subagent_agent_types(cfg, [], {})["r"]["tools"]}
    assert "web_search" in names  # web group survived; unknown mcp/pack refs dropped without error


def test_load_plugin_tools_by_pack_groups_by_name(tmp_path):
    from kokua.core.build import _load_plugin_tools_by_pack

    by_pack = _load_plugin_tools_by_pack(_config(tmp_path))  # load_plugins on by default
    assert "example" in by_pack
    assert any(fn.__name__ == "roll_dice" for fn in by_pack["example"])
    assert _load_plugin_tools_by_pack(_config(tmp_path, load_plugins=False)) == {}


async def test_lean_supervisor_drops_worker_tools(tmp_path):
    # memory=True so the memory tools are present and we can confirm they STAY on the lean supervisor.
    flat = await Assistant.create(_config(tmp_path, memory=True), FakeChannel(), client=MockAsyncModelClient([]))
    lean = await Assistant.create(
        _config(tmp_path, memory=True, lean_supervisor=True), FakeChannel(), client=MockAsyncModelClient([])
    )
    flat_names = {fn.__name__ for fn in flat._agent.tools}
    lean_names = {fn.__name__ for fn in lean._agent.tools}
    assert lean_names < flat_names  # strictly smaller
    # The delegate + date/time + cross-cutting tools stay; worker/built-in/plugin tools are gone.
    assert {"spawn_subagent", "get_current_date_and_time"} <= lean_names
    for kept in ("author_skill", "add_mcp_server", "store_memory", "update_config"):
        assert kept in lean_names
    for gone in ("web_search", "read_file", "calculate", "echo", "roll_dice"):
        assert gone not in lean_names, gone
        assert gone in flat_names, gone


async def test_lean_worker_receives_boot_connected_mcp_server(tmp_path, monkeypatch):
    """Boot reorder: config [[mcp.server]] servers connect before the first agent is built, so a lean
    supervisor's worker role that names one receives its tools (and the supervisor itself does not)."""
    import kokua.core.build as build_mod
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("get_quote")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    captured = {}

    def fake_make(model, *, agent_types, tool_approval, **kwargs):
        captured["agent_types"] = agent_types

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """menu"""
            return "ok"

        spawn_subagent.__name__ = "spawn_subagent"
        spawn_subagent.__tool_is_async__ = True
        spawn_subagent.__tool_is_streaming__ = False
        spawn_subagent.__tool_spec__ = {"function": {"name": "spawn_subagent"}}
        return spawn_subagent

    monkeypatch.setattr(build_mod, "make_async_subagent_tool", fake_make)

    cfg = _config(
        tmp_path,
        lean_supervisor=True,
        mcp_servers=[MCPServerConfig(url="https://broker/mcp", name="stocks")],
        subagent_roles={"trader": {"description": "Trades.", "mcp_servers": ["stocks"]}},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    trader_tools = {fn.__name__ for fn in captured["agent_types"]["trader"]["tools"]}
    assert "get_quote" in trader_tools  # worker got the boot-connected server's tool
    assert "get_quote" not in {fn.__name__ for fn in assistant._agent.tools}  # not on the lean supervisor


def _capturing_subagent_factory(captured: list):
    """A make_async_subagent_tool stand-in that records each build's agent_types and returns a
    spawn_subagent stub (so rebuild_subagent_tool can find and replace it by name)."""

    def fake_make(model, *, agent_types, tool_approval, **kwargs):
        captured.append(agent_types)

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """menu"""
            return "ok"

        spawn_subagent.__name__ = "spawn_subagent"
        spawn_subagent.__tool_is_async__ = True
        spawn_subagent.__tool_is_streaming__ = False
        spawn_subagent.__tool_spec__ = {"function": {"name": "spawn_subagent"}}
        return spawn_subagent

    return fake_make


async def test_runtime_added_mcp_server_reaches_lean_worker(tmp_path, monkeypatch):
    """Rebuild trigger: adding an MCP server at runtime rebuilds spawn_subagent, so a lean worker role
    that names the server gets its tools without a restart -- and the raw tools stay off the supervisor."""
    import kokua.core.build as build_mod

    captured: list = []
    monkeypatch.setattr(build_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("get_quote")]), "none"))
    )

    cfg = _config(
        tmp_path,
        lean_supervisor=True,
        subagent_roles={"trader": {"description": "Trades.", "mcp_servers": ["https://broker/mcp"]}},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    assert captured[-1]["trader"]["tools"] == []  # server not connected at create -> worker has nothing yet

    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")

    assert "get_quote" in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}  # worker got it via rebuild
    assert "get_quote" not in {fn.__name__ for fn in assistant._agent.tools}  # supervisor stayed lean


async def test_runtime_added_mcp_server_reaches_all_lean_conversations(tmp_path, monkeypatch):
    """The rebuild fans out: a runtime add updates the spawn_subagent of EVERY live conversation's
    supervisor, not just the one whose add_mcp_server ran."""
    import kokua.core.build as build_mod

    captured: list = []
    monkeypatch.setattr(build_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("get_quote")]), "none"))
    )

    cfg = _config(
        tmp_path,
        lean_supervisor=True,
        subagent_roles={"trader": {"description": "Trades.", "mcp_servers": ["https://broker/mcp"]}},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    await assistant.new_conversation()  # a second live conversation/agent
    await assistant.select_conversation(first)
    assert len(assistant._registry.live_agents()) == 2

    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")

    # The add's refresh rebuilt spawn_subagent on both agents -> the two most recent builds both have it.
    recent = captured[-2:]
    assert all("get_quote" in {fn.__name__ for fn in c["trader"]["tools"]} for c in recent)


async def test_runtime_removed_mcp_server_drops_from_lean_worker(tmp_path, monkeypatch):
    import kokua.core.build as build_mod

    captured: list = []
    monkeypatch.setattr(build_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("get_quote")]), "none"))
    )

    cfg = _config(
        tmp_path,
        lean_supervisor=True,
        subagent_roles={"trader": {"description": "Trades.", "mcp_servers": ["https://broker/mcp"]}},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")
    assert "get_quote" in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}

    remove_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    await remove_mcp(url="https://broker/mcp")
    assert "get_quote" not in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}  # worker dropped it


def test_lean_supervisor_on_by_default():

    assert AssistantConfig().lean_supervisor is True  # production default


def test_resolve_system_message_selects_supervisor_guidance(tmp_path):
    from kokua.core.build import resolve_system_message
    from kokua.config.schema import SUBAGENT_GUIDANCE, SUPERVISOR_GUIDANCE

    lean = resolve_system_message(_config(tmp_path, lean_supervisor=True))
    assert SUPERVISOR_GUIDANCE.strip() in lean
    assert SUBAGENT_GUIDANCE.strip() not in lean
    flat = resolve_system_message(_config(tmp_path))
    assert SUBAGENT_GUIDANCE.strip() in flat
    assert SUPERVISOR_GUIDANCE.strip() not in flat


async def test_lean_supervisor_requires_subagents_falls_back_to_flat(tmp_path):
    # lean_supervisor without subagents is meaningless (no delegate), so wiring falls back to flat:
    # the supervisor keeps the built-in worker tools it would otherwise delegate.
    a = await Assistant.create(
        _config(tmp_path, lean_supervisor=True, subagents=False), FakeChannel(), client=MockAsyncModelClient([])
    )
    names = {fn.__name__ for fn in a._agent.tools}
    assert "web_search" in names  # flat groups present
    assert "spawn_subagent" not in names  # subagents off, so no delegate


def test_flattened_by_pack_matches_flat_plugin_tools(tmp_path):
    # The flat supervisor mounts _dedup_by_name(by_pack.values()); it must equal the original flat
    # plugin-tool set so flat-mode wiring is unchanged (packs are now built once, shared).
    from kokua.core.build import _dedup_by_name, _load_plugin_tools, _load_plugin_tools_by_pack

    cfg = _config(tmp_path)
    flat = {fn.__name__ for fn in _load_plugin_tools(cfg)}
    flattened = {fn.__name__ for fn in _dedup_by_name(_load_plugin_tools_by_pack(cfg).values())}
    assert flattened == flat


async def test_subagent_tool_routes_approval_to_parent(tmp_path, monkeypatch):
    import kokua.core.build as build_mod

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


async def test_assistant_wires_author_skill_tool(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    tools = assistant._agent.tools
    author = next((t for t in tools if t.__name__ == "author_skill"), None)
    assert author is not None and author.__tool_is_async__ is True

    await author(name="format-standup", description="Format a standup update.", body="# Standup\n\nDo X.")
    assert (cfg.skills_dir / "format-standup" / "SKILL.md").exists()
    assert "format-standup" in assistant._agent.skill_manager.skills


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


async def test_document_tools_round_trip(tmp_path):
    """The document tools are wired to a working DocumentStore (pure-Python, hermetic)."""
    assistant = await Assistant.create(_config(tmp_path, memory=True), FakeChannel(), client=MockAsyncModelClient([]))
    tools = {fn.__name__: fn for fn in assistant._agent.tools}
    assert tools["save_document"]("/notes/standup.md", "Yesterday, Today, Blockers") == "Saved /notes/standup.md."
    assert tools["read_document"]("/notes/standup.md") == "Yesterday, Today, Blockers"


def test_build_memory_tools_carry_dispatch_attrs(tmp_path):
    """build_memory returns AIMU's memory + document tools directly (no wrapping); they carry the
    dispatch attributes AIMU needs. Thread-safety now lives inside the stores (aimu.memory), not here."""
    from kokua.core.build import build_memory

    _, _, tools = build_memory(_config(tmp_path, memory=True))
    assert tools
    for fn in tools:
        assert fn.__name__
        assert hasattr(fn, "__tool_spec__")


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


def test_make_agent_builder_wires_and_restores(tmp_path):
    from aimu.sessions import Session, TinyDBSessionStore
    from kokua.core.build import build_memory, make_agent_builder

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
        reapply_config=noop,
    )
    agent = build("c1")
    assert agent.tool_approval is not None
    tool_names = {getattr(t, "__name__", None) for t in agent.tools}
    assert "author_skill" in tool_names
    # Messages for this conversation were restored onto the fresh agent's client.
    assert any(m.get("content") == "hello" for m in agent.model_client.messages)
