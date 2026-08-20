"""Building an agent from its ``[agents.*]`` declaration: toolsets, delegation, memory, and skills."""

from __future__ import annotations


import pytest


from kokua.config import MCPServerConfig
from kokua.config.schema import AgentConfig
from kokua.core.assistant import Assistant
from kokua.toolsets.context import LiveState
from tests.channels import FakeChannel, _config, example_agents
from tests.fakes import _FakeMCP, _await_value, _fake_mcp_tool, _offline_until_connected
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


def _specs(config, state=None) -> dict[str, dict]:
    """The AIMU ``agent_types`` the entry agent's delegate is built with, for this config."""
    from kokua.toolsets.agents import build_agent_specs, build_registry

    if state is None:
        state = LiveState(config=config, registry=build_registry(config))
    return build_agent_specs(config, state, config.entry_agent)


def test_the_shipped_agents_give_their_workers_the_tools_they_declare(tmp_path):
    """The [agents.*] tables Kokua ships are what a real install runs, so the toolsets they name have to
    resolve to actual tools on the workers the entry agent delegates to."""
    worker_tools = {fn.__name__ for spec in _specs(_config(tmp_path)).values() for fn in spec["tools"]}
    # The groups the shipped workers declare are present...
    assert {"get_weather", "read_file", "calculate", "get_current_date_and_time", "convert_time"} <= worker_tools
    # ...and the generative toolsets, which no shipped agent names and nothing adds in code, are not.
    assert "generate_image" not in worker_tools


async def test_assistant_wires_subagent_tool(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert "spawn_subagent" in names


async def test_subagent_tool_is_typed_with_the_agents_the_entry_agent_delegates_to(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    spawn = next(t for t in assistant._agent.tools if t.__name__ == "spawn_subagent")
    # Typed mode takes (agent_type, task); the docstring lists the configured targets.
    import inspect

    params = list(inspect.signature(spawn).parameters)
    assert params[:2] == ["agent_type", "task"]
    assert "researcher" in spawn.__doc__ and "coder" in spawn.__doc__


def test_a_worker_resolves_a_group_an_mcp_server_and_a_plugin_from_one_list(tmp_path):
    """One namespace, one list: an agent names a built-in group, a configured MCP server, and a plugin
    toolset the same way, and receives the tools of all three."""
    from types import SimpleNamespace

    from kokua.toolsets.agents import build_registry
    from kokua.toolsets.registry import Toolset

    def stock_quote():  # fake MCP tool callable -- the resolver only reads __name__
        pass

    def make_pdf():  # fake plugin-toolset tool
        pass

    cfg = _config(
        tmp_path,
        agents={
            "assistant": AgentConfig(tools=["time"], delegates_to=["trader"]),
            "trader": AgentConfig(description="Trades.", tools=["compute", "stocks", "pdf"]),
        },
        mcp_servers=[MCPServerConfig(url="https://broker/mcp", name="stocks")],
    )
    connections = [SimpleNamespace(url="https://broker/mcp", callables=[stock_quote])]
    # A registry standing in for the real one: everything real except a fake "pdf" toolset, so the test
    # can tell a plugin's tools apart from a built-in group's without depending on the real pdf
    # toolset's actual output.
    registry = build_registry(cfg)
    registry["pdf"] = Toolset(name="pdf", description="fake plugin", build=lambda ctx: [make_pdf])
    state = LiveState(config=cfg, connections=connections, registry=registry)
    names = {fn.__name__ for fn in _specs(cfg, state)["trader"]["tools"]}
    assert "stock_quote" in names  # the named MCP server's tool
    assert "make_pdf" in names  # the plugin toolset's tool
    assert "calculate" in names  # the built-in compute group's tool


# --- Startup validation: every invalid config fails here, naming the offending value ----------


async def test_no_configured_agents_refuses_to_start(tmp_path):
    """Agents exist only in config.toml, so a config with none would start something that looks running
    and has no capability at all. Refuse instead, and name the command that fixes it."""
    from kokua.config import ConfigError

    with pytest.raises(ConfigError, match="at least one"):
        await Assistant.create(_config(tmp_path, agents={}), FakeChannel(), client=MockAsyncModelClient([]))


async def test_an_unknown_toolset_name_refuses_to_start(tmp_path):
    """A misspelled toolset used to leave the agent quietly smaller than the config said."""
    from kokua.config import ConfigError

    cfg = _config(tmp_path, agents={"assistant": AgentConfig(tools=["bogus"])})
    with pytest.raises(ConfigError, match="bogus"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_an_unresolvable_declared_model_refuses_to_start(tmp_path):
    """A worker's model is only reached when something delegates to it, so an unchecked typo would
    surface mid-turn as a failed spawn rather than at startup."""
    from kokua.config import ConfigError

    cfg = _config(tmp_path, agents={"assistant": AgentConfig(tools=[], model="nonsense:whatever")})
    with pytest.raises(ConfigError, match=r"\[agents.assistant\].model"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_a_missing_entry_agent_refuses_to_start(tmp_path):
    from kokua.config import ConfigError

    cfg = _config(tmp_path, entry_agent="supervisor")
    with pytest.raises(ConfigError, match="supervisor"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_a_delegation_cycle_refuses_to_start(tmp_path):
    """A delegate is built by recursing into its targets, so a cycle would exhaust the stack at startup."""
    from kokua.config import ConfigError

    cfg = _config(
        tmp_path,
        agents={
            "assistant": AgentConfig(tools=["time"], delegates_to=["helper"]),
            "helper": AgentConfig(tools=["fs"], delegates_to=["assistant"]),
        },
    )
    with pytest.raises(ConfigError, match="cycle"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_a_toolset_name_two_providers_claim_refuses_to_start_as_a_config_error(tmp_path):
    """The registry reports a collision with its own ``ToolsetError``, but from a user's side a duplicate
    [[mcp.server]].name is a config mistake like any other, so startup presents the whole family as
    ``ConfigError`` rather than leaking an internal exception type a front end does not catch."""
    from kokua.config import ConfigError

    cfg = _config(
        tmp_path,
        agents={"assistant": AgentConfig(tools=["shared"])},
        mcp_servers=[
            MCPServerConfig(url="https://one/mcp", name="shared"),
            MCPServerConfig(url="https://two/mcp", name="shared"),
        ],
    )
    with pytest.raises(ConfigError, match="shared"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


# The entry agent's COMPLETE toolset, grouped by where each group is built. Half of it comes from AIMU,
# which is why no naming convention in this repo can answer "where are all the tools?" on its own. This
# is the executable half of the inventory table in docs/explanation/architecture.md: adding or removing a
# tool the shipped entry agent holds fails the test below, and the table needs the same edit in that
# commit.
ENTRY_AGENT_TOOLS = {
    "kokua toolsets/conversations.py": {"list_conversations", "read_conversation", "search_conversations"},
    "kokua toolsets/config.py": {"read_config", "update_config"},
    "kokua toolsets/mcp_admin.py": {"add_mcp_server", "remove_mcp_server"},
    "kokua toolsets/capabilities.py": {"list_capabilities", "compose_worker"},
    "kokua toolsets/scheduling.py": {
        "schedule_task",
        "list_scheduled_tasks",
        "get_scheduled_task",
        "update_scheduled_task",
        "cancel_scheduled_task",
        "enable_scheduled_task",
        "disable_scheduled_task",
        "run_scheduled_task",
        "stop_scheduled_task",
    },
    "aimu make_skill_authoring_tool / make_skill_script_tool": {"author_skill", "add_skill_script"},
    "aimu builtin.time": {"get_current_date_and_time", "convert_time"},
    "aimu make_async_subagent_tool": {"spawn_subagent"},
}
# Present because the shipped entry agent declares the `memory` and `documents` toolsets, so they are
# asserted separately from the rest below.
STORE_TOOLS = {
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
    for source, names in {**ENTRY_AGENT_TOOLS, **STORE_TOOLS}.items():
        if name in names:
            return source
    return "unknown source"


def _without(*toolsets: str) -> dict[str, AgentConfig]:
    """The shipped agents with ``toolsets`` struck from the entry agent's declaration."""
    agents = example_agents()
    entry = agents["assistant"]
    entry.tools = [name for name in entry.tools if name not in toolsets]
    return agents


async def test_entry_agent_toolset_is_exactly_the_documented_inventory(tmp_path):
    """The entry agent's tool context is the point of the design: what the shipped [agents.assistant]
    table declares, and nothing else. It holds what mutates shared state (skills, MCP, memory, config,
    scheduling), reading its other conversations, the clock, and the delegate; the built-in groups and
    plugin toolsets are declared by the workers instead.

    Asserted as an exact set rather than a sample, so a plugin toolset leaking onto the entry agent fails
    here, and so does adding a tool without updating the inventory table this list mirrors."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    expected = _expected(ENTRY_AGENT_TOOLS, STORE_TOOLS)

    leaked = sorted(names - expected)
    missing = sorted(expected - names)
    assert not leaked, f"undocumented tools on the entry agent: {leaked} (a worker tool leaking here?)"
    assert not missing, f"documented tools absent: {[(n, _source_of(n)) for n in missing]}"


async def test_undeclaring_the_store_toolsets_drops_exactly_their_tools(tmp_path):
    """Declaration is the only switch: strike `memory` and `documents` from the entry agent's list and it
    loses exactly their tools, with nothing else disturbed."""
    config = _config(tmp_path, agents=_without("memory", "documents"))
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert names == _expected(ENTRY_AGENT_TOOLS)


CONVERSATION_TOOL_NAMES = ENTRY_AGENT_TOOLS["kokua toolsets/conversations.py"]


async def test_create_registers_the_conversation_tools(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert CONVERSATION_TOOL_NAMES <= {getattr(fn, "__name__", None) for fn in assistant._agent.tools}


def test_conversation_tools_do_not_reach_the_shipped_workers(tmp_path):
    """Reading other conversations is meaningless to a worker (it shares no history and has no
    conversation identity), so no shipped worker declares that toolset."""
    specs = _specs(_config(tmp_path))
    assert specs  # the shipped entry agent delegates, so this is not vacuous
    for name, spec in specs.items():
        assert CONVERSATION_TOOL_NAMES.isdisjoint({fn.__name__ for fn in spec["tools"]}), name


async def test_worker_receives_boot_connected_mcp_server(tmp_path, monkeypatch):
    """Boot reorder: config [[mcp.server]] servers connect before the first agent is built, so a worker
    declaring one receives its tools (and the entry agent, which does not declare it, does not)."""
    import kokua.toolsets.agents as agents_mod
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

    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", fake_make)

    cfg = _config(tmp_path, **_trading_via("stocks", url="https://broker/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    trader_tools = {fn.__name__ for fn in captured["agent_types"]["trader"]["tools"]}
    assert "get_quote" in trader_tools  # the worker got the boot-connected server's tool
    assert "get_quote" not in {fn.__name__ for fn in assistant._agent.tools}  # not on the entry agent


def _trading_via(name: str, *, url: str) -> dict:
    """Config overrides for an entry agent delegating to one worker that declares the server ``name``."""
    return {
        "agents": {
            "assistant": AgentConfig(tools=["mcp-admin", "time"], delegates_to=["trader"]),
            "trader": AgentConfig(description="Trades.", tools=[name]),
        },
        "mcp_servers": [MCPServerConfig(url=url, name=name)],
    }


def _capturing_subagent_factory(captured: list):
    """A make_async_subagent_tool stand-in that records each build's agent_types and returns a
    spawn_subagent stub (so rebuild_delegation_tool can find and replace it by name)."""

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


async def test_runtime_added_mcp_server_reaches_the_worker_that_declares_it(tmp_path, monkeypatch):
    """Rebuild trigger: connecting a configured-but-offline server at runtime rebuilds spawn_subagent, so
    the worker declaring it gets its tools without a restart -- and the raw tools stay off the entry
    agent."""
    import kokua.toolsets.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    _offline_until_connected(monkeypatch)

    cfg = _config(tmp_path, **_trading_via("stocks", url="https://broker/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    # The server is declared but not connected at create, so the worker has none of its tools yet.
    assert "get_quote" not in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}

    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")

    assert "get_quote" in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}  # worker got it via rebuild
    assert "get_quote" not in {fn.__name__ for fn in assistant._agent.tools}  # entry agent stayed lean


async def test_runtime_added_mcp_server_reaches_all_live_conversations(tmp_path, monkeypatch):
    """The rebuild fans out: a runtime connect updates the spawn_subagent of EVERY live conversation's
    agent, not just the one whose add_mcp_server ran."""
    import kokua.toolsets.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    _offline_until_connected(monkeypatch)

    cfg = _config(tmp_path, **_trading_via("stocks", url="https://broker/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    await assistant.new_conversation()  # a second live conversation/agent
    await assistant.select_conversation(first)
    assert len(assistant._registry.live_agents()) == 2

    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    captured.clear()
    await add_mcp(url="https://broker/mcp")

    # One rebuild per live agent, each carrying the newly connected server's tools.
    assert len(captured) == 2
    assert all("get_quote" in {fn.__name__ for fn in c["trader"]["tools"]} for c in captured)


async def test_runtime_removed_mcp_server_drops_from_the_worker(tmp_path, monkeypatch):
    import kokua.toolsets.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    _offline_until_connected(monkeypatch)

    cfg = _config(tmp_path, **_trading_via("stocks", url="https://broker/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")
    assert "get_quote" in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}

    remove_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    await remove_mcp(url="https://broker/mcp")
    assert "get_quote" not in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}  # worker dropped it


async def test_subagent_tool_routes_approval_to_parent(tmp_path, monkeypatch):
    import kokua.toolsets.agents as agents_mod

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

    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", fake_make_async_subagent_tool)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert captured["tool_approval"] == assistant._approve


async def test_concurrent_tools_flag_reaches_agent(tmp_path):
    on = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert on._agent.concurrent_tool_calls is True
    off = await Assistant.create(
        _config(tmp_path, concurrent_tools=False), FakeChannel(), client=MockAsyncModelClient([])
    )
    assert off._agent.concurrent_tool_calls is False


async def test_assistant_wires_author_skill_tool(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    tools = assistant._agent.tools
    author = next((t for t in tools if t.__name__ == "author_skill"), None)
    assert author is not None and author.__tool_is_async__ is True

    await author(name="format-standup", description="Format a standup update.", body="# Standup\n\nDo X.")
    assert (cfg.skills_dir / "format-standup" / "SKILL.md").exists()
    assert "format-standup" in assistant._agent.skill_manager.skills


async def test_declaring_the_store_toolsets_wires_both_stores(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert _MEMORY_TOOL_NAMES <= names


async def test_memory_stores_exist_when_an_agent_declares_them(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant._memory_store is not None
    assert assistant._document_store is not None


def test_memory_stores_are_not_built_until_a_toolset_asks(tmp_path):
    """The laziness is the mechanism, so it is asserted directly: nothing constructs a store on disk
    just because a LiveState exists."""
    state = LiveState(config=_config(tmp_path))
    assert "memory_store" not in state.__dict__
    assert "document_store" not in state.__dict__


async def test_no_store_declaration_omits_the_tools_and_the_stores(tmp_path):
    config = _config(tmp_path, agents=_without("memory", "documents"))
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent.tools}
    assert _MEMORY_TOOL_NAMES.isdisjoint(names)
    assert assistant._memory_store is None
    assert assistant._document_store is None


async def test_document_tools_round_trip(tmp_path):
    """The document tools are wired to a working DocumentStore (pure-Python, hermetic)."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    tools = {fn.__name__: fn for fn in assistant._agent.tools}
    assert tools["save_document"]("/notes/standup.md", "Yesterday, Today, Blockers") == "Saved /notes/standup.md."
    assert tools["read_document"]("/notes/standup.md") == "Yesterday, Today, Blockers"


def test_memory_toolset_tools_carry_dispatch_attrs(tmp_path):
    """The registry's memory + documents toolsets return AIMU's tools directly (no wrapping); they carry
    the dispatch attributes AIMU needs. Thread-safety lives inside the stores (aimu.memory), not here."""
    from kokua.toolsets import ToolsetContext
    from kokua.toolsets.agents import build_registry

    config = _config(tmp_path)
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


async def test_skill_authored_in_one_conversation_is_visible_in_another(tmp_path):
    """LiveState.skill_manager is one manager shared across every conversation's agent, deliberately:
    skills are files in one user-owned directory, so a skill taught in one conversation should be usable
    in every other one, not just the one that authored it."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first_agent = assistant._agent
    author = next(t for t in first_agent.tools if t.__name__ == "author_skill")
    await author(name="shared-skill", description="Shared.", body="# Shared\n\nDo X.")

    await assistant.new_conversation()
    second_agent = assistant._agent
    assert second_agent.skill_manager is first_agent.skill_manager
    assert "shared-skill" in second_agent.skill_manager.skills


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
    import kokua.toolsets.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _observer_capturing_factory(captured))

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assert captured and captured[-1] is assistant._subagent_reporter


async def test_runtime_mcp_rebuild_keeps_the_activity_reporter(tmp_path, monkeypatch):
    """A runtime MCP add rebuilds spawn_subagent; dropping the observer there would silently stop
    sub-agent display for the rest of the process.

    The captures are cleared and counted rather than just read at [-1]: the boot build already handed the
    reporter over, so a bare last-element check would hold even if the add rebuilt nothing at all."""
    import kokua.toolsets.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _observer_capturing_factory(captured))
    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("get_quote")]), "none"))
    )
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    captured.clear()
    await add_mcp(url="https://broker/mcp")

    assert len(captured) == 1  # one rebuild, for the one live agent
    assert captured[-1] is assistant._subagent_reporter


def test_wire_agent_uses_an_injected_client_as_is(tmp_path):
    """The client injection seam that lets the whole suite run without a model: a caller-supplied
    client is the client the agent ends up with, untouched. Rebuilding it, or assembling and applying a
    fresh system message on top of it, would silently overwrite whatever the injecting caller built it
    with (e.g. a mock's canned responses, or a model switch's already-restored messages).
    """
    from kokua.config.schema import AssistantConfig
    from kokua.core.build import wire_agent
    from kokua.toolsets.agents import build_registry

    config = AssistantConfig(
        data_dir=tmp_path,
        agents={"assistant": AgentConfig(tools=["time"])},
        entry_agent="assistant",
        load_plugins=False,
    )
    state = LiveState(config=config, registry=build_registry(config))
    client = MockAsyncModelClient([])
    client._system_message = "Injected system message."

    agent = wire_agent(config, state, "assistant", client=client)

    assert agent.model_client is client
    assert agent.model_client.system_message == "Injected system message."


def _captured_client_model(monkeypatch, config, build) -> object:
    """The model string ``build`` resolves, with client construction stubbed out."""
    from aimu import aio

    captured = []

    def fake_client(model, system=None):
        captured.append(model)
        return MockAsyncModelClient([])

    monkeypatch.setattr(aio, "client", fake_client)
    build()
    return captured[0]


def _live_state(config) -> LiveState:
    from kokua.toolsets.agents import build_registry

    return LiveState(config=config, registry=build_registry(config))


def test_an_agents_client_is_built_with_the_model_it_declares(tmp_path, monkeypatch):
    from kokua.core.build import wire_agent

    agents = example_agents()
    agents["assistant"].model = "ollama:qwen3:32b"
    config = _config(tmp_path, agents=agents, model="ollama:qwen3:8b")
    state = _live_state(config)
    model = _captured_client_model(monkeypatch, config, lambda: wire_agent(config, state, "assistant"))
    assert model == "ollama:qwen3:32b"


def test_an_agent_declaring_no_model_is_built_with_the_default(tmp_path, monkeypatch):
    from kokua.core.build import wire_agent

    config = _config(tmp_path, model="ollama:qwen3:8b")
    state = _live_state(config)
    model = _captured_client_model(monkeypatch, config, lambda: wire_agent(config, state, "assistant"))
    assert model == "ollama:qwen3:8b"


def test_an_agent_is_wired_with_the_thinking_level_it_declares(tmp_path):
    from kokua.core.build import wire_agent

    agents = example_agents()
    agents["assistant"].thinking = "high"
    config = _config(tmp_path, agents=agents, thinking="low")
    agent = wire_agent(config, _live_state(config), "assistant", client=MockAsyncModelClient([]))
    assert agent.thinking == "high"


def _captured_client(monkeypatch, build):
    """The client ``build`` constructs, with AIMU's factory stubbed out."""
    from aimu import aio

    captured = []

    def fake_client(model, system=None):
        client = MockAsyncModelClient([])
        client.default_generate_kwargs = {}
        captured.append(client)
        return client

    monkeypatch.setattr(aio, "client", fake_client)
    build()
    return captured[0]


def test_a_client_is_built_with_the_generation_parameters_its_agent_resolves(tmp_path, monkeypatch):
    from kokua.core.build import build_model_client

    agents = example_agents()
    agents["assistant"].generation = {"temperature": 0.2}
    config = _config(tmp_path, agents=agents, generation={"temperature": 0.7, "context_length": 32768})
    client = _captured_client(monkeypatch, lambda: build_model_client(config, "sys", "assistant"))
    assert client.default_generate_kwargs == {"temperature": 0.2, "context_length": 32768}


def test_a_client_is_left_untouched_when_nothing_is_declared(tmp_path, monkeypatch):
    """The regression that matters: this tier shadows the model card, so absent must stay absent."""
    from kokua.core.build import build_model_client

    config = _config(tmp_path)
    client = _captured_client(monkeypatch, lambda: build_model_client(config, "sys", "assistant"))
    assert client.default_generate_kwargs == {}


def test_an_agent_declaring_no_thinking_is_wired_with_the_default(tmp_path):
    from kokua.core.build import wire_agent

    config = _config(tmp_path, thinking="medium")
    agent = wire_agent(config, _live_state(config), "assistant", client=MockAsyncModelClient([]))
    assert agent.thinking == "medium"


def test_an_agent_declaring_thinking_off_overrides_the_default(tmp_path):
    from kokua.core.build import wire_agent

    agents = example_agents()
    agents["assistant"].thinking = False
    config = _config(tmp_path, agents=agents, thinking="high")
    agent = wire_agent(config, _live_state(config), "assistant", client=MockAsyncModelClient([]))
    assert agent.thinking is False


def test_an_agent_is_wired_with_no_thinking_when_nothing_is_declared(tmp_path):
    """Absent means AIMU's own default, which it documents as byte-for-byte unchanged requests."""
    from kokua.core.build import wire_agent

    config = _config(tmp_path)
    agent = wire_agent(config, _live_state(config), "assistant", client=MockAsyncModelClient([]))
    assert agent.thinking is None


def test_validate_model_string_rejects_one_this_process_cannot_build():
    """No install has a "bogus-provider", so this is the rejection any typo'd provider gets, carrying
    AIMU's own message -- which names the providers whose extras *are* installed."""
    from kokua.core.build import ModelClientError, validate_model_string

    with pytest.raises(ModelClientError, match="bogus-provider"):
        validate_model_string("bogus-provider:whatever")


def test_validate_model_string_accepts_one_the_client_factory_accepts(monkeypatch):
    """It answers by building a throwaway client, which is what makes a pass here mean the same startup
    will succeed: it is the call `build_model_client` makes."""
    from aimu import aio
    from kokua.core.build import validate_model_string

    seen = []
    monkeypatch.setattr(aio, "client", lambda model, system=None: seen.append(model))
    validate_model_string("ollama:qwen3.8:27b")
    assert seen == ["ollama:qwen3.8:27b"]
