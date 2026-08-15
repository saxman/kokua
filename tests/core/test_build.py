"""Building an agent: tool groups, sub-agent roles, the lean supervisor, memory, and skills."""

from __future__ import annotations


import pytest


from kokua.config import MCPServerConfig
from kokua.core.assistant import Assistant
from kokua.toolsets.context import LiveState
from tests.channels import FakeChannel, _config, example_subagent_roles
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


def test_builtin_groups_reach_the_workers(tmp_path):
    """[tools].groups is the workers' ceiling, not the supervisor's toolset, so the default groups have
    to be verified where they actually land."""
    from kokua.core.build import _build_subagent_agent_types

    worker_tools = {
        fn.__name__ for spec in _build_subagent_agent_types(_config(tmp_path)).values() for fn in spec["tools"]
    }
    # Default groups are present...
    assert {"get_weather", "read_file", "calculate", "get_current_date_and_time", "convert_time"} <= worker_tools
    # ...and the generative groups (opt-in, need AIMU_*_MODEL) are not.
    assert "generate_image" not in worker_tools


async def test_tools_none_leaves_workers_with_nothing_and_the_supervisor_intact(tmp_path):
    from kokua.core.build import _build_subagent_agent_types

    config = _config(tmp_path, tools=["none"])
    worker_tools = {fn.__name__ for spec in _build_subagent_agent_types(config).values() for fn in spec["tools"]}
    assert worker_tools == set()  # not even the ambient clock, which is gated on the global set

    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert {"author_skill", "add_skill_script", "add_mcp_server"} <= names  # cross-cutting tools remain


async def test_assistant_wires_subagent_tool(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "spawn_subagent" in names


async def test_subagent_tool_is_typed_when_roles_are_configured(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    spawn = next(t for t in assistant._agent.tools if t.__name__ == "spawn_subagent")
    # Typed mode takes (agent_type, task); the docstring lists the configured roles.
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


def test_configured_roles_nonempty_when_tools_all(tmp_path):
    from kokua.core.build import _build_subagent_agent_types

    cfg = _config(tmp_path, tools=["all"])
    types = _build_subagent_agent_types(cfg)
    assert types["coder"]["tools"]  # non-empty: fs+compute groups now enabled
    assert any(fn.__name__ == "execute_python" for fn in types["coder"]["tools"])
    assert types["generalist"]["tools"]  # non-empty: all groups enabled


def test_every_role_gets_the_time_tools_whatever_its_own_groups(tmp_path):
    """A worker that cannot tell the time is broken whatever its domain, so the time group is added to
    every role rather than each role having to remember to ask for it."""
    from kokua.core.build import _build_subagent_agent_types

    cfg = _config(
        tmp_path,
        tools=["fs", "compute", "time"],
        subagent_roles={**example_subagent_roles(), "trader": {"description": "Trades.", "groups": ["compute"]}},
    )
    types = _build_subagent_agent_types(cfg)
    # `coder` names fs+compute and `trader` names only compute; neither asks for the time group.
    for role in ("coder", "trader"):
        names = {fn.__name__ for fn in types[role]["tools"]}
        assert {"get_current_date_and_time", "convert_time"} <= names, role
        assert "echo" not in names, role  # the clock arrives without misc riding along


def test_a_role_with_no_sources_still_gets_the_time_tools(tmp_path):
    """A role defined only by a tool-pack or MCP server (no `groups` at all) is the case that silently
    produced a clockless worker."""
    from kokua.core.build import _build_subagent_agent_types

    cfg = _config(tmp_path, subagent_roles={"reporter": {"description": "Writes reports."}})
    names = {fn.__name__ for fn in _build_subagent_agent_types(cfg)["reporter"]["tools"]}
    assert names == {"get_current_date_and_time", "convert_time"}


def test_disabling_the_time_group_withholds_it_from_every_role(tmp_path):
    """The ambient add is gated on the global set, so it never grants a tool the user turned off."""
    from kokua.core.build import _build_subagent_agent_types

    cfg = _config(tmp_path, tools=["fs", "compute"])
    types = _build_subagent_agent_types(cfg)
    for role in ("coder", "generalist"):
        names = {fn.__name__ for fn in types[role]["tools"]}
        assert "get_current_date_and_time" not in names, role
        assert "convert_time" not in names, role


def test_worker_role_resolves_mcp_and_tool_pack_sources(tmp_path):
    from types import SimpleNamespace

    from kokua.core.build import _build_subagent_agent_types
    from kokua.config import MCPServerConfig
    from kokua.toolsets.agents import build_registry
    from kokua.toolsets.registry import Toolset

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
    # A registry standing in for the real one: the real "compute" group plus a fake "pdf" pack, so the
    # test can tell a tool-pack's tools apart from a built-in group's without depending on the real pdf
    # toolset's actual output.
    registry = build_registry(cfg)
    registry["pdf"] = Toolset(name="pdf", description="fake pack", build=lambda ctx: [make_pdf])
    state = LiveState(config=cfg, connections=connections, registry=registry)
    names = {fn.__name__ for fn in _build_subagent_agent_types(cfg, state)["trader"]["tools"]}
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
    state = LiveState(config=cfg, connections=connections)
    types = _build_subagent_agent_types(cfg, state)
    assert [fn.__name__ for fn in types["r"]["tools"]] == ["remote_tool"]


def test_worker_role_unknown_sources_drop_silently(tmp_path):
    from kokua.core.build import _build_subagent_agent_types

    cfg = _config(
        tmp_path,
        tools=["all"],
        subagent_roles={"r": {"groups": ["web"], "mcp_servers": ["nope"], "tool_packs": ["ghost"]}},
    )
    names = {fn.__name__ for fn in _build_subagent_agent_types(cfg)["r"]["tools"]}
    assert "web_search" in names  # web group survived; unknown mcp/pack refs dropped without error


async def test_no_configured_roles_refuses_to_start(tmp_path):
    """Roles are the assistant's only route to a domain tool, so a config with none would start
    something that looks running and cannot browse, read a file, or compute. Refuse instead, and name
    the command that fixes it."""
    from kokua.config import ConfigError

    with pytest.raises(ConfigError, match="subagents.roles"):
        await Assistant.create(_config(tmp_path, subagent_roles={}), FakeChannel(), client=MockAsyncModelClient([]))


def test_supervisor_prompt_names_the_typed_delegate(tmp_path):
    """The prompt names the call it wants the model to make, and there is only one delegate shape."""
    from kokua.core.build import resolve_system_message

    assert "spawn_subagent(agent_type, task)" in resolve_system_message(_config(tmp_path))


# The supervisor's COMPLETE toolset, grouped by where each group is built. Half of it comes from AIMU,
# which is why no naming convention in this repo can answer "where are all the tools?" on its own. This
# is the executable half of the inventory table in docs/explanation/architecture.md: adding or removing a
# supervisor tool fails the test below, and the table needs the same edit in that commit.
SUPERVISOR_TOOLS = {
    "kokua core/tools.py": {"list_conversations", "read_conversation", "search_conversations"},
    "kokua config/tools.py": {"read_config", "update_config"},
    "kokua mcp/tools.py": {"add_mcp_server", "remove_mcp_server"},
    "kokua scheduling/tools.py": {
        "schedule_task",
        "list_scheduled_tasks",
        "get_scheduled_task",
        "update_scheduled_task",
        "cancel_scheduled_task",
        "enable_scheduled_task",
        "disable_scheduled_task",
        "run_scheduled_task",
    },
    "aimu make_skill_authoring_tool / make_skill_script_tool": {"author_skill", "add_skill_script"},
    "aimu builtin.time": {"get_current_date_and_time", "convert_time"},
    "aimu make_async_subagent_tool": {"spawn_subagent"},
}
# Only present with memory enabled, so they are asserted separately below.
MEMORY_TOOLS = {
    "aimu make_memory_tools + make_document_tools": {
        "store_memory",
        "search_memories",
        "list_memories",
        "save_document",
        "read_document",
        "list_documents",
        "search_documents",
    },
}


def _expected(*groups: dict) -> set[str]:
    return {name for group in groups for names in group.values() for name in names}


def _source_of(name: str) -> str:
    for source, names in {**SUPERVISOR_TOOLS, **MEMORY_TOOLS}.items():
        if name in names:
            return source
    return "unknown source"


async def test_supervisor_toolset_is_exactly_the_documented_inventory(tmp_path):
    """The supervisor's tool context is the point of the design: it holds what mutates shared state
    (skills, MCP, memory, config, scheduling), reading its other conversations, the clock, and the
    delegate. Built-in groups and tool-packs live on the workers.

    Asserted as an exact set rather than a sample, so a tool-pack leaking onto the supervisor fails here,
    and so does adding a supervisor tool without updating the inventory table this list mirrors."""
    # memory=True so the memory tools are present and we can confirm they STAY on the supervisor.
    assistant = await Assistant.create(_config(tmp_path, memory=True), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    expected = _expected(SUPERVISOR_TOOLS, MEMORY_TOOLS)

    leaked = sorted(names - expected)
    missing = sorted(expected - names)
    assert not leaked, f"undocumented tools on the supervisor: {leaked} (a worker tool leaking here?)"
    assert not missing, f"documented tools absent: {[(n, _source_of(n)) for n in missing]}"


async def test_disabling_memory_drops_exactly_the_memory_tools(tmp_path):
    assistant = await Assistant.create(_config(tmp_path, memory=False), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert names == _expected(SUPERVISOR_TOOLS)


CONVERSATION_TOOL_NAMES = SUPERVISOR_TOOLS["kokua core/tools.py"]


async def test_create_registers_the_conversation_tools(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert CONVERSATION_TOOL_NAMES <= {getattr(fn, "__name__", None) for fn in assistant._agent.tools}


def test_conversation_tools_do_not_reach_the_workers(tmp_path):
    """Reading other conversations is supervisor-only: a worker shares no history and has no conversation
    identity, so the capability is meaningless to it (and would widen a spawn's blast radius)."""
    from kokua.core.build import _build_subagent_agent_types

    config = _config(tmp_path)
    agent_types = _build_subagent_agent_types(config)
    assert agent_types  # roles are configured, so this is not vacuous
    for role, spec in agent_types.items():
        assert CONVERSATION_TOOL_NAMES.isdisjoint({fn.__name__ for fn in spec["tools"]}), role


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
        subagent_roles={"trader": {"description": "Trades.", "mcp_servers": ["https://broker/mcp"]}},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    # Server not connected at create -> the worker has none of its tools yet (only the ambient clock,
    # which every role gets regardless of its own groups).
    assert "get_quote" not in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}

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
        subagent_roles={"trader": {"description": "Trades.", "mcp_servers": ["https://broker/mcp"]}},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")
    assert "get_quote" in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}

    remove_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    await remove_mcp(url="https://broker/mcp")
    assert "get_quote" not in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}  # worker dropped it


def test_resolve_system_message_always_carries_supervisor_guidance(tmp_path):
    """Delegation is the only route to a domain tool, so there is no configuration under which the
    model should be told anything else."""
    from kokua.core.build import resolve_system_message
    from kokua.config.schema import SUPERVISOR_GUIDANCE

    assert SUPERVISOR_GUIDANCE.strip() in resolve_system_message(_config(tmp_path))


def test_supervisor_guidance_names_the_conversation_tools():
    """No worker has these tools, so if the guidance stops naming them the capability is orphaned: the
    model delegates "what did we decide last week?" to a worker that cannot answer."""
    from kokua.config.schema import SUPERVISOR_GUIDANCE

    for name in CONVERSATION_TOOL_NAMES:
        assert name in SUPERVISOR_GUIDANCE, name


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


async def test_memory_stores_exist_when_memory_is_on(tmp_path):
    assistant = await Assistant.create(_config(tmp_path, memory=True), FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant._memory_store is not None
    assert assistant._document_store is not None


def test_memory_stores_are_not_built_until_a_toolset_asks(tmp_path):
    """The laziness is the mechanism, so it is asserted directly: nothing constructs a store on disk
    just because a LiveState exists."""
    state = LiveState(config=_config(tmp_path, memory=True))
    assert "memory_store" not in state.__dict__
    assert "document_store" not in state.__dict__


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


def test_memory_toolset_tools_carry_dispatch_attrs(tmp_path):
    """The registry's memory + documents toolsets return AIMU's tools directly (no wrapping); they carry
    the dispatch attributes AIMU needs. Thread-safety lives inside the stores (aimu.memory), not here."""
    from kokua.toolsets import ToolsetContext
    from kokua.toolsets.agents import build_registry

    config = _config(tmp_path, memory=True)
    state = LiveState(config=config, registry=build_registry(config))
    ctx = ToolsetContext(state=state, agent=None)
    tools = state.registry["memory"].build(ctx) + state.registry["documents"].build(ctx)
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
    from kokua.core.build import make_agent_builder
    from kokua.toolsets.agents import build_registry

    config = _config(tmp_path)  # existing helper
    store = TinyDBSessionStore(str(config.sessions_path))
    session = Session(
        key="c1",
        metadata={},
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    )
    store.save(session)

    async def noop(*a, **k):
        return None

    state = LiveState(
        config=config,
        registry=build_registry(config),
        oauth_storage_dir=config.data_dir / "mcp-oauth",
        tool_approval=lambda name, args: True,
        reapply_config=noop,
        for_each_agent=lambda apply: None,
    )
    build = make_agent_builder(
        config,
        state,
        client_factory=lambda cid: MockAsyncModelClient([]),
        store=store,
        images_path=config.images_path,
    )
    agent = build("c1")
    assert agent.tool_approval is not None
    tool_names = {getattr(t, "__name__", None) for t in agent.tools}
    assert "author_skill" in tool_names
    # Messages for this conversation were restored onto the fresh agent's client.
    assert any(m.get("content") == "hello" for m in agent.model_client.messages)


def _observer_capturing_factory(captured: list):
    """A make_async_subagent_tool stand-in that records the `observer` each build received."""

    def fake_make(model, *, agent_types, tool_approval, observer=None, **kwargs):
        captured.append(observer)

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """menu"""
            return "ok"

        spawn_subagent.__name__ = "spawn_subagent"
        spawn_subagent.__tool_is_async__ = True
        spawn_subagent.__tool_is_streaming__ = False
        spawn_subagent.__tool_spec__ = {"function": {"name": "spawn_subagent"}}
        return spawn_subagent

    return fake_make


async def test_spawn_subagent_is_built_with_the_activity_reporter(tmp_path, monkeypatch):
    """The reporter must reach AIMU's factory, or a spawn stays invisible in the UI."""
    import kokua.core.build as build_mod

    captured: list = []
    monkeypatch.setattr(build_mod, "make_async_subagent_tool", _observer_capturing_factory(captured))

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assert captured and captured[-1] is assistant._subagent_reporter


async def test_runtime_mcp_rebuild_keeps_the_activity_reporter(tmp_path, monkeypatch):
    """A runtime MCP add rebuilds spawn_subagent; dropping the observer there would silently stop
    sub-agent display for the rest of the process."""
    import kokua.core.build as build_mod

    captured: list = []
    monkeypatch.setattr(build_mod, "make_async_subagent_tool", _observer_capturing_factory(captured))
    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("get_quote")]), "none"))
    )
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")

    assert captured[-1] is assistant._subagent_reporter
