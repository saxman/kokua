"""Building an agent from its ``[agents.*]`` declaration: toolsets, delegation, memory, and skills."""

from __future__ import annotations

import asyncio

import pytest


from kokua.config import MCPServerConfig
from kokua.config.schema import ALL_TOOLSETS, AgentConfig, ReviewerConfig
from kokua.core.agents import build_registry, deferred_for, resident_for
from kokua.core.assistant import Assistant
from kokua.registry.context import LiveState
from kokua.mcp.auth import OAuthSettings
from tests.channels import AlertCapturingChannel, FakeChannel, _config, example_agents, planning_settings
from tests.fakes import _RequestsToolOnce, _FakeMCP, _await_value, _fake_mcp_tool, _offline_until_connected
from tests.helpers import MockAsyncModelClient, allow_all_gates


_MEMORY_TOOL_NAMES = {
    "store_memory",
    "search_memories",
    "list_memories",
    "save_document",
    "read_document",
    "edit_document",
    "list_documents",
    "search_documents",
}


def _specs(config, state=None) -> dict[str, dict]:
    """The AIMU ``agent_types`` the entry agent's delegate is built with, for this config."""
    from kokua.core.agents import build_agent_specs, build_registry

    if state is None:
        state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    return build_agent_specs(config, state, config.entry_agent)


def test_the_shipped_agents_give_their_workers_the_tools_they_declare(tmp_path):
    """The [agents.*] tables Kokua ships are what a real install runs, so the toolsets they name have to
    resolve to actual tools on the workers the entry agent delegates to."""
    worker_tools = {fn.__name__ for spec in _specs(_config(tmp_path)).values() for fn in spec["tools"]}
    # The groups the shipped workers declare are present...
    assert {
        "get_weather",
        "read_file",
        "calculate",
        "run_command",
        "get_current_date_and_time",
        "convert_time",
    } <= worker_tools
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

    from kokua.core.agents import build_registry
    from kokua.registry.registry import Toolset

    def stock_quote():  # fake MCP tool callable -- the resolver only reads __name__
        pass

    def make_pdf():  # fake plugin-toolset tool
        pass

    cfg = _config(
        tmp_path,
        agents={
            "assistant": AgentConfig(available=["time"], delegates_to=["trader"]),
            "trader": AgentConfig(description="Trades.", available=["compute", "stocks", "pdf"]),
        },
        mcp_servers=[MCPServerConfig(url="https://broker/mcp", name="stocks")],
    )
    connections = [SimpleNamespace(url="https://broker/mcp", callables=[stock_quote])]
    # A registry standing in for the real one: everything real except a fake "pdf" toolset, so the test
    # can tell a plugin's tools apart from a built-in group's without depending on the real pdf
    # toolset's actual output.
    registry = build_registry(cfg)
    registry["pdf"] = Toolset(name="pdf", description="fake plugin", build=lambda ctx: [make_pdf])
    state = LiveState(gate_for=allow_all_gates, config=cfg, connections=connections, registry=registry)
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

    cfg = _config(tmp_path, agents={"assistant": AgentConfig(available=["bogus"])})
    with pytest.raises(ConfigError, match="bogus"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_an_unresolvable_declared_model_refuses_to_start(tmp_path):
    """A worker's model is only reached when something delegates to it, so an unchecked typo would
    surface mid-turn as a failed spawn rather than at startup."""
    from kokua.config import ConfigError

    cfg = _config(tmp_path, agents={"assistant": AgentConfig(available=[], model="nonsense:whatever")})
    with pytest.raises(ConfigError, match=r"\[agents.assistant\].model"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_an_unresolvable_reviewer_model_refuses_to_start(tmp_path):
    """[reviewers.plan] is deep planning's critic and nothing [security.auto_approval] names, so this
    proves a reviewer is checked whether or not the gate ever reads it."""
    from kokua.config import ConfigError

    cfg = _config(tmp_path, reviewers={"plan": ReviewerConfig(model="nonsense:whatever")})
    with pytest.raises(ConfigError, match=r"\[reviewers.plan\].model"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_an_unresolvable_approval_reviewer_model_refuses_to_start_with_the_gate_off(tmp_path):
    """The gate being off used to mean a bad [reviewers.approval] model surfaced only on the first
    gated call, as "reviewer could not be reached" with no hint why. It is checked regardless now,
    the same way [agents.*] are checked whether or not anything delegates to them."""
    from kokua.config import ConfigError

    cfg = _config(
        tmp_path,
        auto_approval_enabled=False,
        reviewers={"approval": ReviewerConfig(model="nonsense:whatever")},
    )
    with pytest.raises(ConfigError, match=r"\[reviewers.approval\].model"):
        await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))


async def test_a_valid_declared_reviewer_model_passes(tmp_path):
    cfg = _config(tmp_path, reviewers={"plan": ReviewerConfig(model="ollama:qwen3:32b")})
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant is not None


async def test_a_reviewer_model_may_carry_an_endpoint_override(tmp_path):
    """A reviewer's model accepts the same extended string an agent's does, endpoint included."""
    cfg = _config(
        tmp_path,
        reviewers={"plan": ReviewerConfig(model="ollama:qwen3.5:9b@http://example.local:11434")},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant is not None


async def test_a_reviewer_declaring_no_model_is_not_checked(tmp_path):
    """A reviewer with no declared model inherits [assistant].model, which is validated on its own
    path; re-checking the inherited value here would risk blaming the wrong table for someone else's
    typo. [assistant].model is left unset here too, so this also proves the missing declaration is
    never resolved just to be checked."""
    cfg = _config(tmp_path, reviewers={"plan": ReviewerConfig()})
    assert cfg.model is None
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant is not None


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
            "assistant": AgentConfig(available=["time"], delegates_to=["helper"]),
            "helper": AgentConfig(available=["fs"], delegates_to=["assistant"]),
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
        agents={"assistant": AgentConfig(available=["shared"])},
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
    "kokua toolsets/conversations.py": {
        "list_conversations",
        "read_conversation",
        "search_conversations",
        "rename_conversation",
        "export_conversation",
    },
    "kokua toolsets/config.py": {"read_config", "update_config"},
    "kokua toolsets/mcp.py": {"add_mcp_server", "remove_mcp_server"},
    "kokua toolsets/capabilities.py": {"list_capabilities", "compose_subagent"},
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
    "aimu make_skill_authoring_tool / make_skill_update_tool / make_skill_script_tool": {
        "author_skill",
        "update_skill",
        "add_skill_script",
    },
    "aimu builtin.time": {"get_current_date_and_time", "convert_time"},
    "aimu make_async_subagent_tool": {"spawn_subagent"},
    "kokua toolsets/benchmark.py": {"benchmark_model"},
    "kokua toolsets/messaging.py": {"send_message", "list_agents"},
}
# Present because the shipped entry agent declares the `memory` and `documents` toolsets, so they are
# asserted separately from the rest below.
STORE_TOOLS = {
    "aimu make_memory_tools": {"store_memory", "search_memories", "list_memories"},
    "aimu make_document_tools": {
        "save_document",
        "read_document",
        "edit_document",
        "list_documents",
        "search_documents",
    },
}
# The sources above whose toolsets the shipped entry agent defers: absent from its tools until it loads
# them, and present in the inventory because they are still part of what it can reach.
DEFERRED_SOURCES = {
    "aimu make_document_tools",
    "kokua toolsets/mcp.py",
    "kokua toolsets/scheduling.py",
    "kokua toolsets/benchmark.py",
    "aimu make_skill_authoring_tool / make_skill_update_tool / make_skill_script_tool",
}
# What AIMU adds to an agent holding at least one deferred toolset, so the model can see and load them.
LOADER_TOOLS = {"available_tools", "load_tools"}


async def _loaded_tools(assistant, *toolsets: str) -> list:
    """The entry agent's tools after loading ``toolsets``, the way a model's `load_tools` call would."""
    for toolset in toolsets:
        assert (await assistant._agent.load_tool_group(toolset)).startswith(f"Loaded '{toolset}':")
    return assistant._agent._effective_tools(None)


def _expected(*groups: dict) -> set[str]:
    return {name for group in groups for names in group.values() for name in names}


def _resident_expected() -> set[str]:
    """What the shipped entry agent's tools hold at wiring: the inventory minus its deferred sources, plus the loaders."""
    inventory = {**ENTRY_AGENT_TOOLS, **STORE_TOOLS}
    resident = {source: names for source, names in inventory.items() if source not in DEFERRED_SOURCES}
    return _expected(resident) | LOADER_TOOLS


def _deferred_expected() -> set[str]:
    inventory = {**ENTRY_AGENT_TOOLS, **STORE_TOOLS}
    return _expected({source: names for source, names in inventory.items() if source in DEFERRED_SOURCES})


def _source_of(name: str) -> str:
    for source, names in {**ENTRY_AGENT_TOOLS, **STORE_TOOLS}.items():
        if name in names:
            return source
    return "unknown source"


def _without(*toolsets: str) -> dict[str, AgentConfig]:
    """The shipped agents with ``toolsets`` struck from the entry agent's declaration."""
    agents = example_agents()
    entry = agents["assistant"]
    entry.available = [name for name in entry.available if name not in toolsets]
    return agents


def test_shipped_entry_agent_resident_set(tmp_path):
    config = _config(tmp_path)
    assert resident_for(config, build_registry(config), "assistant") == [
        "memory",
        "config",
        "conversations",
        "planning",
        "capabilities",
        "time",
        "messaging",
    ]
    assert deferred_for(config, build_registry(config), "assistant") == [
        "documents",
        "skills",
        "mcp",
        "scheduling",
        "benchmark",
    ]


async def test_entry_agent_toolset_is_exactly_the_documented_inventory(tmp_path):
    """The entry agent's tool context is the point of the design: what the shipped [agents.assistant]
    table makes resident, and nothing else. It holds from its first turn what it uses on most turns
    (memory, config, its other conversations, the clock, the delegate, messaging), and meets the rest
    (documents, skills, MCP, scheduling, `benchmark`) through `available_tools` / `load_tools`; the
    built-in groups and the remaining shipped toolsets are declared by the workers instead, or by nobody.

    Asserted as an exact set rather than a sample, so a plugin toolset leaking onto the entry agent fails
    here, and so does adding a tool without updating the inventory table this list mirrors. The deferred
    half is asserted absent, so a toolset quietly becoming resident fails too."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in assistant._agent._effective_tools(None)}
    expected = _resident_expected()

    leaked = sorted(names - expected)
    missing = sorted(expected - names)
    assert not leaked, f"undocumented tools on the entry agent: {leaked} (a worker tool leaking here?)"
    assert not missing, f"documented tools absent: {[(n, _source_of(n)) for n in missing]}"
    assert names.isdisjoint(_deferred_expected())
    assistant._state.close()


async def test_loading_every_deferred_toolset_completes_the_documented_inventory(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    for toolset in deferred_for(assistant._config, assistant._state.registry, "assistant"):
        assert (await assistant._agent.load_tool_group(toolset)).startswith(f"Loaded '{toolset}':")
    names = {fn.__name__ for fn in assistant._agent._effective_tools(None)}
    assert names == _expected(ENTRY_AGENT_TOOLS, STORE_TOOLS) | LOADER_TOOLS
    assistant._state.close()


async def test_undeclaring_the_store_toolsets_drops_exactly_their_tools(tmp_path):
    """Declaration is the only switch: strike `memory` and `documents` from the entry agent's list and it
    loses exactly their tools, with nothing else disturbed."""
    config = _config(tmp_path, agents=_without("memory", "documents"))
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    # Undeclared, not merely deferred: `documents` is in neither list, so no load can bring it back.
    assert "documents" not in resident_for(assistant._config, assistant._state.registry, "assistant")
    assert "documents" not in deferred_for(assistant._config, assistant._state.registry, "assistant")
    for toolset in deferred_for(assistant._config, assistant._state.registry, "assistant"):
        await assistant._agent.load_tool_group(toolset)
    names = {fn.__name__ for fn in assistant._agent._effective_tools(None)}
    inventory = {**ENTRY_AGENT_TOOLS, **STORE_TOOLS}
    without_stores = {source: tools for source, tools in inventory.items() if source not in STORE_TOOLS}
    assert names == _expected(without_stores) | LOADER_TOOLS
    assistant._state.close()


CONVERSATION_TOOL_NAMES = ENTRY_AGENT_TOOLS["kokua toolsets/conversations.py"]


async def test_create_registers_the_conversation_tools(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert CONVERSATION_TOOL_NAMES <= {getattr(fn, "__name__", None) for fn in assistant._agent.tools}


def test_only_the_introspector_reads_history_among_the_shipped_workers(tmp_path):
    """The rule is about conversation identity, not about workers. Reading the user's other
    conversations is meaningless to a worker that has no relationship to any of them, which is every
    shipped worker except the one spawned to evaluate a conversation: `introspector` is handed a
    conversation as its subject, so history is its whole subject matter. Pinning both halves is the
    point, since the interesting failure is the toolset spreading to workers that have no such
    subject."""
    specs = _specs(_config(tmp_path))
    assert specs  # the shipped entry agent delegates, so this is not vacuous
    assert CONVERSATION_TOOL_NAMES <= {fn.__name__ for fn in specs["introspector"]["tools"]}
    for name, spec in specs.items():
        if name == "introspector":
            continue
        assert CONVERSATION_TOOL_NAMES.isdisjoint({fn.__name__ for fn in spec["tools"]}), name


async def test_worker_receives_boot_connected_mcp_server(tmp_path, monkeypatch):
    """Boot reorder: config [[mcp.server]] servers connect before the first agent is built, so a worker
    declaring one receives its tools (and the entry agent, which does not declare it, does not)."""
    import kokua.core.agents as agents_mod
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
    await assistant.start()
    trader_tools = {fn.__name__ for fn in captured["agent_types"]["trader"]["tools"]}
    assert "get_quote" in trader_tools  # the worker got the boot-connected server's tool
    assert "get_quote" not in {fn.__name__ for fn in assistant._agent.tools}  # not on the entry agent


def _trading_via(name: str, *, url: str) -> dict:
    """Config overrides for an entry agent delegating to one worker that declares the server ``name``.

    ``confirm_tools`` is emptied because these two agents provide none of the four tools the shipped
    default gates, and a gate naming no tool is a startup error.
    """
    return {
        "agents": {
            "assistant": AgentConfig(available=["mcp", "time"], delegates_to=["trader"]),
            "trader": AgentConfig(description="Trades.", available=[name]),
        },
        "mcp_servers": [MCPServerConfig(url=url, name=name)],
        "default_confirm": [],
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
    import kokua.core.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    _offline_until_connected(monkeypatch)

    cfg = _config(tmp_path, **_trading_via("stocks", url="https://broker/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    # The server is declared but not connected at create, so the worker has none of its tools yet.
    assert "get_quote" not in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}

    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")

    assert "get_quote" in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}  # worker got it via rebuild
    assert "get_quote" not in {fn.__name__ for fn in assistant._agent.tools}  # entry agent stayed lean


async def test_runtime_added_mcp_server_reaches_all_live_conversations(tmp_path, monkeypatch):
    """The rebuild fans out: a runtime connect updates the spawn_subagent of EVERY live conversation's
    agent, not just the one whose add_mcp_server ran."""
    import kokua.core.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    _offline_until_connected(monkeypatch)

    cfg = _config(tmp_path, **_trading_via("stocks", url="https://broker/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
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
    import kokua.core.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    _offline_until_connected(monkeypatch)

    cfg = _config(tmp_path, **_trading_via("stocks", url="https://broker/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    add_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_mcp(url="https://broker/mcp")
    assert "get_quote" in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}

    remove_mcp = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    await remove_mcp(url="https://broker/mcp")
    assert "get_quote" not in {fn.__name__ for fn in captured[-1]["trader"]["tools"]}  # worker dropped it


async def test_subagent_tool_routes_approval_to_parent(tmp_path, monkeypatch):
    import kokua.core.agents as agents_mod

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
    await assistant.start()
    gate = captured["tool_approval"]
    # The factory-level gate is the default tier's; each worker spec carries its own, bound to its name.
    assert gate.func == assistant._human.approve and gate.args == ("[assistant.tools]",)


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

    tools = await _loaded_tools(assistant, "skills")
    author = next((t for t in tools if t.__name__ == "author_skill"), None)
    assert author is not None and author.__tool_is_async__ is True

    await author(name="format-standup", description="Format a standup update.", body="# Standup\n\nDo X.")
    assert (cfg.skills_dir / "format-standup" / "SKILL.md").exists()
    assert "format-standup" in assistant._agent.skill_manager.skills


async def test_declaring_the_store_toolsets_wires_both_stores(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    names = {fn.__name__ for fn in await _loaded_tools(assistant, "documents")}
    assert _MEMORY_TOOL_NAMES <= names


async def test_memory_stores_exist_when_an_agent_declares_them(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    await _loaded_tools(assistant, "documents")
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
    names = {fn.__name__ for fn in assistant._agent._effective_tools(None)}
    assert _MEMORY_TOOL_NAMES.isdisjoint(names)
    # Deferred would also leave the tools and stores unbuilt, so undeclared is asserted separately.
    assert "documents" not in assistant._config.agents["assistant"].available
    assert "load_tools" in names
    assert (await assistant._agent.load_tool_group("documents")).startswith("No tool group named 'documents'")
    assert assistant._memory_store is None
    assert assistant._document_store is None


async def test_document_tools_round_trip(tmp_path):
    """The document tools are wired to a working DocumentStore (pure-Python, hermetic)."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    tools = {fn.__name__: fn for fn in await _loaded_tools(assistant, "documents")}
    assert tools["save_document"]("/notes/standup.md", "Yesterday, Today, Blockers") == "Saved /notes/standup.md."
    assert tools["read_document"]("/notes/standup.md") == "Yesterday, Today, Blockers"


def test_memory_toolset_tools_carry_dispatch_attrs(tmp_path):
    """The registry's memory + documents toolsets return AIMU's tools directly (no wrapping); they carry
    the dispatch attributes AIMU needs. Thread-safety lives inside the stores (aimu.memory), not here."""
    from kokua.registry import ToolsetContext
    from kokua.core.agents import build_registry

    config = _config(tmp_path)
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    ctx = ToolsetContext(state=state, agent=None, agent_name="assistant")
    tools = state.registry["memory"].build(ctx) + state.registry["documents"].build(ctx)
    assert tools
    for fn in tools:
        assert fn.__name__
        assert hasattr(fn, "__tool_spec__")


async def test_assistant_can_fix_a_skill_it_authored(tmp_path):
    """The loop the `skills` toolset exists for only closes if the prose is editable too.

    `author_skill` refuses to clobber, so before `update_skill` was wired here the assistant's only
    move on a skill that turned out wrong was to author a second one under a different name. Asserted
    through the live agent rather than the toolset, because what is under test is the wiring: the tool
    has to reach the same `SkillManager` the entry agent's catalogue is built from, or the skill it
    just fixed reads back as not found.
    """
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    tools = await _loaded_tools(assistant, "skills")
    author = next(t for t in tools if t.__name__ == "author_skill")
    update = next(t for t in tools if t.__name__ == "update_skill")

    await author(name="standup", description="First try.", body="# Standup\n\nThree bullets.")
    msg = await update(skill_name="standup", body="# Standup\n\nFour bullets, and a blocker.")

    assert "standup" in msg
    skill = assistant._state.skill_manager.skills["standup"]
    assert skill.load_body() == "# Standup\n\nFour bullets, and a blocker."
    assert skill.description == "First try."  # untouched: an update writes only what it is given


async def test_assistant_authors_and_registers_runnable_script(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    tools = await _loaded_tools(assistant, "skills")
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
    author = next(t for t in await _loaded_tools(assistant, "skills") if t.__name__ == "author_skill")
    await author(name="shared-skill", description="Shared.", body="# Shared\n\nDo X.")

    await assistant.new_conversation()
    second_agent = assistant._agent
    assert second_agent.skill_manager is first_agent.skill_manager
    assert "shared-skill" in second_agent.skill_manager.skills


def test_make_agent_builder_wires_and_restores(tmp_path):
    from aimu.sessions import Session, TinyDBSessionStore
    from kokua.core.build import make_agent_builder
    from kokua.core.agents import build_registry

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
        oauth=OAuthSettings(storage_dir=config.data_dir / "mcp-oauth"),
        gate_for=lambda agent: lambda name, args: True,
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
    assert "store_memory" in tool_names
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
    import kokua.core.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _observer_capturing_factory(captured))

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    assert captured and captured[-1] is assistant._subagent_reporter


async def test_runtime_mcp_rebuild_keeps_the_activity_reporter(tmp_path, monkeypatch):
    """A runtime MCP add rebuilds spawn_subagent; dropping the observer there would silently stop
    sub-agent display for the rest of the process.

    The captures are cleared and counted rather than just read at [-1]: the boot build already handed the
    reporter over, so a bare last-element check would hold even if the add rebuilt nothing at all."""
    import kokua.core.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _observer_capturing_factory(captured))
    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp", lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("get_quote")]), "none"))
    )
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    add_mcp = next(t for t in await _loaded_tools(assistant, "mcp") if getattr(t, "__name__", "") == "add_mcp_server")
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
    from kokua.core.agents import build_registry

    config = AssistantConfig(
        data_dir=tmp_path,
        agents={"assistant": AgentConfig(available=["time"])},
        entry_agent="assistant",
    )
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
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
    from kokua.core.agents import build_registry

    return LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))


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


def test_the_entry_agent_is_built_with_its_own_resolved_cap(tmp_path):
    """The only per-agent-resolved site: the entry agent is an [agents.*] entry like any other, which is
    why nothing here needs a special case for it."""
    from kokua.config.schema import AssistantConfig
    from kokua.core.agents import build_registry
    from kokua.core.build import wire_agent

    config = AssistantConfig(
        data_dir=tmp_path,
        agents={"assistant": AgentConfig(available=[], max_iterations=30)},
        entry_agent="assistant",
        max_iterations=10,
    )
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    agent = wire_agent(config, state, "assistant", client=MockAsyncModelClient([]))
    assert agent.max_iterations == 30


def test_the_entry_agent_falls_back_to_the_global_cap(tmp_path):
    from kokua.config.schema import AssistantConfig
    from kokua.core.agents import build_registry
    from kokua.core.build import wire_agent

    config = AssistantConfig(
        data_dir=tmp_path,
        agents={"assistant": AgentConfig(available=[])},
        entry_agent="assistant",
        max_iterations=15,
    )
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    agent = wire_agent(config, state, "assistant", client=MockAsyncModelClient([]))
    assert agent.max_iterations == 15


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


def test_the_entry_agent_runs_skill_scripts_with_the_host_environment(tmp_path):
    """A ``SkillAgent`` builds its own skills server, so the ``env`` ``LiveState.skill_tools`` passes
    when it builds one for a spawned worker never reaches this agent's scripts. Wiring that forgets
    raises nothing: the `email-report` script simply reports itself unconfigured while `[email]` is
    filled in, and `markdown-to-pdf` writes outside the served downloads folder.
    """
    from kokua.core.build import wire_agent

    config = _config(tmp_path, email_host="smtp.example.com", email_to="user@example.com")
    state = _live_state(config)

    agent = wire_agent(config, state, "assistant", client=MockAsyncModelClient([]))

    assert agent.script_env == state.script_env()
    assert agent.script_env["KOKUA_EMAIL_HOST"] == "smtp.example.com"
    assert agent.script_env["KOKUA_DOWNLOADS_DIR"] == str(config.downloads_path)


def test_a_toolset_is_told_which_agent_it_is_building_for(tmp_path):
    """`ctx.agent_name` is the identity a toolset scoping itself to its holder needs, and unlike
    `ctx.agent` it is known at every construction site. `benchmark` uses it to ask the config what its own
    holder runs on, which is a question a live client cannot answer: nothing on one retains the model
    string it was built from."""
    from kokua.core.agents import build_registry
    from kokua.core.build import wire_agent
    from kokua.registry.registry import Toolset

    holders: list[str] = []

    def record(ctx) -> list:
        holders.append(ctx.agent_name)
        return []

    cfg = _config(tmp_path, agents={"assistant": AgentConfig(available=["probe"])})
    registry = build_registry(cfg)
    registry["probe"] = Toolset(name="probe", description="records its holder", build=record)
    state = LiveState(gate_for=allow_all_gates, config=cfg, registry=registry)

    wire_agent(cfg, state, cfg.entry_agent, client=MockAsyncModelClient([]))

    assert holders == [cfg.entry_agent]


# --- Residency: a deferred toolset is built when the model loads it, a resident one at wiring ----------

_WEB_GUIDANCE_SNIPPET = "training cutoff"


def _residency_config(tmp_path, *, available=("memory", "time", "web"), resident=("memory", "time"), **overrides):
    # No default gate: the shipped one names toolsets this small agent does not declare, which startup
    # refuses, and these tests are about residency rather than approval.
    overrides.setdefault("default_confirm", [])
    return _config(
        tmp_path,
        agents={"assistant": AgentConfig(available=list(available), resident=list(resident))},
        **overrides,
    )


def _wired(config, state=None):
    from kokua.core.agents import build_registry
    from kokua.core.build import wire_agent

    if state is None:
        state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    return wire_agent(config, state, "assistant", client=MockAsyncModelClient([])), state


def _tool_names(agent) -> list[str]:
    return [fn.__name__ for fn in agent._effective_tools(None)]


async def test_a_deferred_toolset_is_not_in_the_entry_agents_tools_until_loaded(tmp_path):
    agent, _ = _wired(_residency_config(tmp_path))
    names = _tool_names(agent)
    assert "get_web_content" not in names and "load_tools" in names
    assert (await agent.load_tool_group("web")).startswith("Loaded 'web':")
    assert "get_web_content" in _tool_names(agent)


async def test_the_prompt_names_deferred_toolsets_and_omits_their_guidance(tmp_path):
    from kokua.core.build import entry_agent_system_message

    config = _residency_config(tmp_path)
    _, state = _wired(config)
    message = entry_agent_system_message(config, state)
    assert "Also available, load before use: web." in message
    assert _WEB_GUIDANCE_SNIPPET not in message


async def test_the_prompt_has_no_names_line_when_nothing_is_deferred(tmp_path):
    from kokua.core.build import entry_agent_system_message

    config = _residency_config(tmp_path, resident=("memory", "time", "web"))
    _, state = _wired(config)
    message = entry_agent_system_message(config, state)
    assert "load before use" not in message
    assert _WEB_GUIDANCE_SNIPPET in message


async def test_loading_returns_the_toolsets_guidance(tmp_path):
    agent, _ = _wired(_residency_config(tmp_path))
    assert _WEB_GUIDANCE_SNIPPET in await agent.load_tool_group("web")


async def test_calling_a_deferred_tool_before_loading_points_at_loading(tmp_path):
    config = _residency_config(tmp_path)
    client = _RequestsToolOnce("get_web_content", {"url": "https://example.com"})
    assistant = await Assistant.create(config, FakeChannel(), client=client)
    await assistant.start()

    await assistant._agent.run("go")

    results = [m["content"] for m in client.messages if m.get("role") == "tool"]
    assert results and "call available_tools to see what you can load, then load_tools" in results[-1]
    assert "get_web_content" not in _tool_names(assistant._agent)


async def test_two_conversations_load_independently(tmp_path):
    from kokua.core.build import wire_agent

    config = _residency_config(tmp_path)
    first, state = _wired(config)
    second = wire_agent(config, state, "assistant", client=MockAsyncModelClient([]))
    await first.load_tool_group("web")
    assert "get_web_content" in _tool_names(first)
    assert "get_web_content" not in _tool_names(second)


def _registry_with_broken_toolset(config, builds: list):
    from kokua.core.agents import build_registry
    from kokua.registry.registry import Toolset

    def explode(ctx):
        builds.append(1)
        raise RuntimeError("store locked")

    registry = build_registry(config)
    registry["broken"] = Toolset(name="broken", description="always fails", build=explode)
    return registry


async def test_a_failing_deferred_build_reports_to_the_model_and_the_user(tmp_path, monkeypatch):
    import asyncio

    config = _residency_config(tmp_path, available=("time", "broken"), resident=("time",))
    builds: list = []
    monkeypatch.setattr("kokua.core.agents.validated_registry", lambda cfg: _registry_with_broken_toolset(cfg, builds))
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(config, channel, client=MockAsyncModelClient([]))
    await assistant.start()

    assert (await assistant._agent.load_tool_group("broken")) == "Could not load 'broken': store locked"
    for _ in range(20):
        if channel.alerts:
            break
        await asyncio.sleep(0.01)
    assert [text for text, *_ in channel.alerts] == ["Loading broken for assistant failed: store locked"]

    await assistant._agent.load_tool_group("broken")
    assert len(builds) == 2  # the group stays loadable, so a second load retries the build


async def test_alert_soon_is_a_no_op_before_the_assistant_has_started(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assistant._state.alert_soon("nobody is listening yet")  # must not raise
    assistant._state.close()


def test_a_deferred_toolset_carrying_a_workflow_still_offers_its_command(tmp_path):
    from kokua.core.agents import build_command_map, build_registry

    config = _residency_config(
        tmp_path, available=("time", "planning"), resident=("time",), toolset_settings=planning_settings()
    )
    commands = build_command_map(config, build_registry(config))
    assert "plan" in commands


def test_an_agent_declaring_no_resident_list_has_no_deferred_group(tmp_path):
    agent, _ = _wired(_config(tmp_path, agents={"assistant": AgentConfig(available=["time"])}))
    assert "load_tools" not in _tool_names(agent)


# --- Deferred loads go through the approval hook, and fail loudly and recoverably -----------------------


async def test_loading_a_deferred_toolset_resolves_the_approval_entry_waiting_on_it(tmp_path):
    config = _residency_config(
        tmp_path, available=("time", "web"), resident=("time",), default_confirm=["web.get_web_content"]
    )
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert "get_web_content" not in assistant._human.policies.for_agent("assistant").confirm

    assert (await assistant._agent.load_tool_group("web")).startswith("Loaded 'web':")

    assert "get_web_content" in assistant._human.policies.for_agent("assistant").confirm
    assistant._state.close()


async def test_a_misnamed_approval_entry_refuses_the_load_and_no_tool_arrives(tmp_path):
    import asyncio

    config = _residency_config(tmp_path, available=("time", "web"), resident=("time",), default_confirm=["web.nope"])
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(config, channel, client=MockAsyncModelClient([]))
    await assistant.start()

    result = await assistant._agent.load_tool_group("web")

    assert result.startswith("Could not load 'web':")
    for _ in range(20):
        if channel.alerts:
            break
        await asyncio.sleep(0.01)
    assert channel.alerts and channel.alerts[0][0].startswith("Loading web for assistant failed:")
    assert "get_web_content" not in _tool_names(assistant._agent)
    assistant._state.close()


async def test_a_deferred_build_sees_the_live_agent(tmp_path):
    """A toolset that needs the agent object (``skills``) must get it when it is loaded, not None."""
    from kokua.core.agents import build_registry
    from kokua.registry.registry import Toolset

    seen: list = []

    def record(ctx) -> list:
        seen.append(ctx.agent)
        return []

    config = _residency_config(tmp_path, available=("time", "probe"), resident=("time",))
    registry = build_registry(config)
    registry["probe"] = Toolset(name="probe", description="records its agent", build=record)
    state = LiveState(gate_for=allow_all_gates, config=config, registry=registry)
    agent, _ = _wired(config, state)

    await agent.load_tool_group("probe")

    assert seen == [agent]


async def test_a_closed_loop_does_not_mask_the_real_build_error(tmp_path, monkeypatch):
    import asyncio

    config = _residency_config(tmp_path, available=("time", "broken"), resident=("time",))
    monkeypatch.setattr("kokua.core.agents.validated_registry", lambda cfg: _registry_with_broken_toolset(cfg, []))
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    closed = asyncio.new_event_loop()
    closed.close()
    assistant._loop = closed

    assert (await assistant._agent.load_tool_group("broken")) == "Could not load 'broken': store locked"
    assistant._state.close()


async def test_pending_alerts_are_cancelled_at_shutdown(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assistant._alert_soon("will not be delivered")
    await asyncio.sleep(0)  # the scheduled call runs and creates the task
    pending = list(assistant._alert_tasks)
    await assistant._cancel_alerts()
    assert all(task.done() for task in pending)
    assert not assistant._alert_tasks
    assistant._state.close()


async def test_a_deferred_mcp_server_that_is_not_connected_refuses_to_load_and_loads_once_it_is(tmp_path):
    from types import SimpleNamespace

    def stock_quote():  # the resolver only reads __name__
        pass

    config = _config(
        tmp_path,
        agents={"assistant": AgentConfig(available=["time", "stocks"], resident=["time"])},
        mcp_servers=[MCPServerConfig(url="https://broker/mcp", name="stocks")],
        default_confirm=[],
    )
    agent, state = _wired(config)

    result = await agent.load_tool_group("stocks")
    assert result.startswith("Could not load 'stocks':") and "not connected" in result

    state.connections.append(SimpleNamespace(url="https://broker/mcp", callables=[stock_quote]))
    assert (await agent.load_tool_group("stocks")).startswith("Loaded 'stocks':")
    assert "stock_quote" in _tool_names(agent)


async def test_a_resident_mcp_server_that_is_not_connected_still_builds_empty(tmp_path):
    config = _config(
        tmp_path,
        agents={"assistant": AgentConfig(available=["time", "stocks"])},
        mcp_servers=[MCPServerConfig(url="https://broker/mcp", name="stocks")],
        default_confirm=[],
    )
    agent, _ = _wired(config)
    assert "load_tools" not in _tool_names(agent)


@pytest.mark.parametrize(
    ("tool_name", "mode"),
    [
        ("available_tools", "deferred"),
        ("load_tools", "deferred"),
        ("spawn_subagent", "delegator"),
        ("activate_skill", "skill"),
    ],
)
async def test_a_resident_mcp_tool_cannot_shadow_agent_core_tools(tmp_path, monkeypatch, tool_name, mode):
    from types import SimpleNamespace

    from tests.core.test_skill_toolsets import write_skill

    async def skip_mcp_reconnect(*args, **kwargs):
        pass

    monkeypatch.setattr("kokua.core.assistant.reconnect_mcp_servers", skip_mcp_reconnect)
    assistant_config = AgentConfig(available=["stocks", "time"], resident=["stocks", "time"])
    agents = {"assistant": assistant_config}
    if mode == "deferred":
        assistant_config.available = ["stocks", "time", "web"]
    elif mode == "delegator":
        assistant_config.delegates_to = ["worker"]
        agents["worker"] = AgentConfig(available=["time"])
    elif mode == "skill":
        assistant_config.available.append("weekly-digest")

    url = "https://broker/mcp"
    config = _config(
        tmp_path,
        agents=agents,
        mcp_servers=[MCPServerConfig(url=url, name="stocks")],
        default_confirm=[],
        default_review=[],
    )
    if mode == "skill":
        write_skill(config, "weekly-digest", "Digest the week.")

    channel = AlertCapturingChannel()
    assistant = await Assistant.create(config, channel, client=MockAsyncModelClient([]))
    mcp_tool = _fake_mcp_tool(tool_name)
    assistant._state.connections.append(SimpleNamespace(url=url, callables=[mcp_tool]))
    try:
        await assistant.start()
        if mode == "skill":
            await assistant._agent._setup_skills_async()

        matches = [tool for tool in assistant._agent._effective_tools(None) if tool.__name__ == tool_name]
        assert len(matches) == 1
        assert matches[0] is not mcp_tool
        assert any(
            f"{tool_name} from 'stocks'" in alert and "core tool" in alert.lower() for alert, *_ in channel.alerts
        )
    finally:
        if mode == "skill" and assistant._started:
            skill_client = assistant._agent._skills_mcp_client
            if skill_client is not None:
                await skill_client.aclose()
        assistant._state.close()


async def test_an_all_available_agent_filters_a_resident_loader_collision(tmp_path, monkeypatch):
    from types import SimpleNamespace

    async def skip_mcp_reconnect(*args, **kwargs):
        pass

    monkeypatch.setattr("kokua.core.assistant.reconnect_mcp_servers", skip_mcp_reconnect)
    url = "https://broker/mcp"
    config = _config(
        tmp_path,
        agents={"assistant": AgentConfig(available=ALL_TOOLSETS, resident=["stocks", "time"])},
        mcp_servers=[MCPServerConfig(url=url, name="stocks")],
    )
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(config, channel, client=MockAsyncModelClient([]))
    mcp_tool = _fake_mcp_tool("load_tools")
    assistant._state.connections.append(SimpleNamespace(url=url, callables=[mcp_tool, _fake_mcp_tool("get_quote")]))
    try:
        await assistant.start()
        matches = [tool for tool in assistant._agent._effective_tools(None) if tool.__name__ == "load_tools"]

        assert len(matches) == 1
        assert matches[0] is not mcp_tool
        assert "get_quote" in {tool.__name__ for tool in assistant._agent._effective_tools(None)}
        assert any("load_tools from 'stocks'" in alert and "core tool" in alert.lower() for alert, *_ in channel.alerts)
    finally:
        assistant._state.close()


def test_a_worker_spec_keeps_its_core_delegation_tool_over_a_resident_mcp_tool(tmp_path):
    from types import SimpleNamespace

    from kokua.core.agents import shadowed_tools

    url = "https://broker/mcp"
    config = _config(
        tmp_path,
        agents={
            "assistant": AgentConfig(available=["time"], delegates_to=["worker"]),
            "worker": AgentConfig(available=["stocks", "time"], resident=["stocks", "time"], delegates_to=["leaf"]),
            "leaf": AgentConfig(available=["time"]),
        },
        mcp_servers=[MCPServerConfig(url=url, name="stocks")],
    )
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    mcp_tool = _fake_mcp_tool("spawn_subagent")
    state.connections.append(SimpleNamespace(url=url, callables=[mcp_tool]))

    worker_tools = _specs(config, state)["worker"]["tools"]
    matches = [tool for tool in worker_tools if tool.__name__ == "spawn_subagent"]

    assert len(matches) == 1
    assert matches[0] is not mcp_tool
    assert any(
        shadow.agent == "worker" and shadow.tool == "spawn_subagent" and shadow.shadowed == "stocks" and shadow.core
        for shadow in shadowed_tools(config, state.registry, state.tools_by_toolset)
    )


def test_the_prompt_wire_agent_builds_for_itself_names_deferred_toolsets_only(tmp_path, monkeypatch):
    captured: list[str] = []

    def fake_client(cfg, system_message, agent_name):
        captured.append(system_message)
        return MockAsyncModelClient([])

    monkeypatch.setattr("kokua.core.build.build_model_client", fake_client)
    from kokua.core.agents import build_registry
    from kokua.core.build import wire_agent

    config = _residency_config(tmp_path)
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    wire_agent(config, state, "assistant")

    assert "Also available, load before use: web." in captured[0]
    assert _WEB_GUIDANCE_SNIPPET not in captured[0]


# --- A rebuilt agent comes back with what its conversation loaded -----------------------------------------


def _load_call(arguments, call_id="a") -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{"type": "function", "id": call_id, "function": {"name": "load_tools", "arguments": arguments}}],
    }


def _load_result(call_id, content) -> dict:
    return {"role": "tool", "name": "load_tools", "content": content, "tool_call_id": call_id}


def _load_exchange(name, call_id="a", *, result=None, arguments=None) -> list[dict]:
    """A stored ``load_tools`` call and the result it got, a successful load unless ``result`` says otherwise."""
    arguments = {"name": name} if arguments is None else arguments
    result = f"Loaded {name.strip()!r}: some_tool." if result is None else result
    return [_load_call(arguments, call_id), _load_result(call_id, result)]


def test_loaded_toolsets_in_reads_load_tools_calls_in_order():
    from kokua.core.build import loaded_toolsets_in

    messages = [
        *_load_exchange("web", "a"),
        *_load_exchange("documents", "b", arguments='{"name": "documents"}'),
        *_load_exchange("web", "c"),
        *_load_exchange("audio", "d", arguments="not json"),
        *_load_exchange("audio", "e", arguments="[1, 2]"),
        *_load_exchange("audio", "f", arguments={"name": 7}),
        *_load_exchange("audio", "g", arguments={}),
        {"role": "tool", "tool_calls": [{"function": {"name": "load_tools", "arguments": {"name": "audio"}}}]},
        {"role": "assistant", "tool_calls": [{"function": {"name": "get_time", "arguments": {"name": "time"}}}]},
        {"role": "assistant", "content": "no calls"},
    ]
    assert loaded_toolsets_in(messages) == ["web", "documents"]


@pytest.mark.parametrize(
    "result",
    [
        "Tool 'load_tools' was not approved.",
        "Tool 'load_tools' was not approved: the user declined",
        "Could not load 'web': the MCP server 'web' (https://x/mcp) is not connected",
        "Tool 'load_tools' raised an error: boom",
        "No tool group named 'web'. Loadable: documents.",
        "Loaded 'documents': read_document.",
    ],
)
def test_a_load_that_did_not_succeed_is_not_restored(result):
    from kokua.core.build import loaded_toolsets_in

    assert loaded_toolsets_in(_load_exchange("web", result=result)) == []


def test_a_load_call_with_no_result_is_not_restored():
    """A turn cancelled mid-call stores the call and never its result."""
    from kokua.core.build import loaded_toolsets_in

    assert loaded_toolsets_in([_load_call({"name": "web"}, "a")]) == []


def test_a_repeat_load_answered_already_loaded_adds_nothing_the_first_load_did_not():
    from kokua.core.build import loaded_toolsets_in

    already = _load_exchange("web", "b", result="'web' is already loaded.")
    assert loaded_toolsets_in(already) == []
    assert loaded_toolsets_in([*_load_exchange("web", "a"), *already]) == ["web"]


def test_a_denied_load_after_a_successful_one_keeps_the_toolset():
    from kokua.core.build import loaded_toolsets_in

    denied = _load_exchange("web", "b", result="Tool 'load_tools' was not approved.")
    assert loaded_toolsets_in([*_load_exchange("web", "a"), *denied]) == ["web"]


async def test_a_real_load_result_is_what_restoring_reads(tmp_path):
    from kokua.core.build import loaded_toolsets_in

    agent, _ = _wired(_residency_config(tmp_path))
    result = await agent.load_tool_group("web")

    assert loaded_toolsets_in([_load_call({"name": "web"}, "a"), _load_result("a", result)]) == ["web"]


def _preload_config(tmp_path, **overrides):
    return _residency_config(
        tmp_path, available=("memory", "time", "documents", "web"), resident=("memory", "time"), **overrides
    )


def _preloaded(config, names, state=None, client=None):
    from kokua.core.agents import build_registry
    from kokua.core.build import wire_agent

    if state is None:
        state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    client = client or MockAsyncModelClient([])
    return wire_agent(config, state, "assistant", client=client, preloaded=names), state


def _prompted_client(config, system_message, agent_name):
    client = MockAsyncModelClient([])
    client.system_message = system_message
    return client


def test_a_rebuilt_agent_has_its_loads_back_in_order(tmp_path):
    config = _preload_config(tmp_path)
    resident_names = _tool_names(_preloaded(config, [])[0])

    agent, _ = _preloaded(config, ["documents", "web"])

    names = _tool_names(agent)
    assert max(names.index(n) for n in resident_names if n in names and n != "spawn_subagent") < names.index(
        "read_document"
    )
    assert names.index("read_document") < names.index("get_web_content")


def test_a_restored_toolset_is_resident_and_not_also_loadable(tmp_path):
    agent, _ = _preloaded(_preload_config(tmp_path), ["web"])

    listing = agent._groups.listing()
    assert "web [resident]" in listing
    assert "documents [loadable]" in listing


def test_the_names_line_leaves_out_restored_toolsets(tmp_path, monkeypatch):
    monkeypatch.setattr("kokua.core.build.build_model_client", _prompted_client)
    from kokua.core.agents import build_registry
    from kokua.core.build import wire_agent

    config = _preload_config(tmp_path)
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    agent = wire_agent(config, state, "assistant", preloaded=["web"])

    message = agent.model_client.system_message
    assert "Also available, load before use: documents." in message
    assert "web" not in message.split("Also available, load before use:")[1]


async def test_a_preloaded_toolsets_approval_entry_is_resolved_before_its_tools_exist(tmp_path):
    config = _preload_config(tmp_path, default_confirm=["web.get_web_content"])

    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        assert "get_web_content" not in assistant._human.policies.for_agent("assistant").confirm

        _preloaded(config, ["web"], state=assistant._state)

        assert "get_web_content" in assistant._human.policies.for_agent("assistant").confirm
    finally:
        assistant._state.close()


def test_a_preload_that_fails_is_skipped_logged_and_still_loadable(tmp_path, caplog):
    config = _residency_config(tmp_path, available=("time", "broken", "web"), resident=("time",))
    builds: list = []
    state = LiveState(gate_for=allow_all_gates, config=config, registry=_registry_with_broken_toolset(config, builds))

    with caplog.at_level("WARNING", logger="kokua.core.build"):
        agent, _ = _preloaded(config, ["broken", "web"], state=state)

    assert "get_web_content" in _tool_names(agent)
    assert "'broken'" in caplog.text and "store locked" in caplog.text
    listing = agent._groups.listing()
    assert "broken [loadable]" in listing and "web [resident]" in listing
    assert builds


def test_a_failed_preloads_name_stays_in_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("kokua.core.build.build_model_client", _prompted_client)
    from kokua.core.build import wire_agent

    config = _residency_config(tmp_path, available=("time", "broken", "web"), resident=("time",))
    state = LiveState(gate_for=allow_all_gates, config=config, registry=_registry_with_broken_toolset(config, []))
    agent = wire_agent(config, state, "assistant", preloaded=["broken", "web"])

    assert "Also available, load before use: broken." in agent.model_client.system_message


def test_a_preload_no_longer_deferred_is_ignored(tmp_path):
    config = _preload_config(tmp_path)
    expected = _tool_names(_preloaded(config, [])[0])

    agent, _ = _preloaded(config, ["memory", "audio", "no-such-toolset", "memory"])

    assert _tool_names(agent) == expected


def test_the_agent_builder_restores_what_the_stored_conversation_loaded(tmp_path):
    from aimu.sessions import Session, TinyDBSessionStore
    from kokua.core.agents import build_registry
    from kokua.core.build import make_agent_builder

    config = _preload_config(tmp_path)
    store = TinyDBSessionStore(str(config.sessions_path))
    store.save(
        Session(
            key="c1",
            metadata={},
            messages=[
                {"role": "user", "content": "hi"},
                *_load_exchange("documents", "a"),
                *_load_exchange("web", "b", arguments='{"name": "web"}'),
            ],
        )
    )
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    build = make_agent_builder(
        config,
        state,
        client_factory=lambda cid: MockAsyncModelClient([]),
        store=store,
        images_path=config.images_path,
    )

    names = _tool_names(build("c1"))

    assert names.index("read_document") < names.index("get_web_content")
    assert "get_web_content" not in _tool_names(build("a-conversation-with-no-session"))


def _rebuilt_from(config, messages, registry=None):
    from aimu.sessions import Session, TinyDBSessionStore
    from kokua.core.agents import build_registry
    from kokua.core.build import make_agent_builder

    store = TinyDBSessionStore(str(config.sessions_path))
    store.save(Session(key="c1", metadata={}, messages=[{"role": "user", "content": "hi"}, *messages]))
    state = LiveState(gate_for=allow_all_gates, config=config, registry=registry or build_registry(config))
    build = make_agent_builder(
        config,
        state,
        client_factory=lambda cid: MockAsyncModelClient([]),
        store=store,
        images_path=config.images_path,
    )
    return build("c1")


def test_a_denied_load_stays_denied_when_the_agent_is_rebuilt(tmp_path):
    """The gate refusing ``load_tools`` stores the call anyway, so restoring from calls alone would hand
    over, at the next restart or eviction, the toolset the user said no to."""
    agent = _rebuilt_from(
        _preload_config(tmp_path),
        [
            *_load_exchange("web", "a", result="Tool 'load_tools' was not approved."),
            *_load_exchange("documents", "b"),
        ],
    )

    names = _tool_names(agent)
    assert "get_web_content" not in names and "read_document" in names
    assert "web [loadable]" in agent._groups.listing()


def test_a_failed_load_is_not_retried_when_the_agent_is_rebuilt(tmp_path):
    config = _residency_config(tmp_path, available=("time", "broken"), resident=("time",))
    builds: list = []

    agent = _rebuilt_from(
        config,
        _load_exchange("broken", result="Could not load 'broken': store locked"),
        registry=_registry_with_broken_toolset(config, builds),
    )

    assert builds == []
    assert "broken [loadable]" in agent._groups.listing()


def test_the_restored_prompt_through_the_production_client_path_names_what_is_still_loadable(tmp_path, monkeypatch):
    from aimu.sessions import Session, TinyDBSessionStore
    from kokua.core.agents import build_registry
    from kokua.core.build import entry_agent_system_message, make_agent_builder

    monkeypatch.setattr("kokua.core.build.build_model_client", _prompted_client)
    config = _preload_config(tmp_path)
    config.agents["assistant"].available.append("weekly-review")
    skill_dir = config.skills_dir / "weekly-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: weekly-review\ndescription: Review the week.\n---\nDo it.\n")
    store = TinyDBSessionStore(str(config.sessions_path))
    store.save(Session(key="c1", metadata={}, messages=[{"role": "user", "content": "hi"}, *_load_exchange("web")]))
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    build = make_agent_builder(
        config,
        state,
        client_factory=lambda cid: _prompted_client(config, entry_agent_system_message(config, state), "assistant"),
        store=store,
        images_path=config.images_path,
    )

    message = build("c1").model_client.system_message

    assert _WEB_GUIDANCE_SNIPPET in message
    assert "web" not in message.split("Also available, load before use:")[1]
    assert "weekly-review" not in message.split("Also available, load before use:")[1]


def test_the_entry_agents_own_prompt_does_not_list_skills_as_loadable(tmp_path):
    from kokua.core.agents import build_registry
    from kokua.core.build import entry_agent_system_message

    config = _preload_config(tmp_path)
    config.agents["assistant"].available.append("weekly-review")
    skill_dir = config.skills_dir / "weekly-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: weekly-review\ndescription: Review the week.\n---\nDo it.\n")
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))

    assert "weekly-review" not in entry_agent_system_message(config, state)


def _registry_with_clock_toolset(config):
    from kokua.core.agents import build_registry
    from kokua.registry.registry import Toolset

    def convert_time():
        return "shadow"

    registry = build_registry(config)
    registry["clock"] = Toolset(name="clock", description="a second clock", build=lambda ctx: [convert_time])
    return registry


def test_a_preload_never_duplicates_a_tool_name_the_agent_already_holds(tmp_path, caplog):
    config = _residency_config(tmp_path, available=("time", "clock"), resident=("time",))
    state = LiveState(gate_for=allow_all_gates, config=config, registry=_registry_with_clock_toolset(config))

    with caplog.at_level("WARNING", logger="kokua.core.build"):
        agent, _ = _preloaded(config, ["clock"], state=state)

    assert _tool_names(agent).count("convert_time") == 1
    assert "convert_time" in caplog.text and "clock" in caplog.text


def test_a_failed_preload_does_not_alert_the_user(tmp_path):
    config = _residency_config(tmp_path, available=("time", "broken"), resident=("time",))
    alerts: list[str] = []
    state = LiveState(gate_for=allow_all_gates, config=config, registry=_registry_with_broken_toolset(config, []))
    state.alert_soon = alerts.append

    _preloaded(config, ["broken"], state=state)

    assert alerts == []


async def test_a_model_driven_load_that_fails_still_alerts_the_user(tmp_path):
    config = _residency_config(tmp_path, available=("time", "broken"), resident=("time",))
    alerts: list[str] = []
    state = LiveState(gate_for=allow_all_gates, config=config, registry=_registry_with_broken_toolset(config, []))
    state.alert_soon = alerts.append
    agent, _ = _preloaded(config, [], state=state)

    await agent.load_tool_group("broken")

    assert alerts == ["Loading broken for assistant failed: store locked"]


def test_preloads_keep_the_order_they_were_loaded_in_not_the_declared_order(tmp_path):
    agent, _ = _preloaded(_preload_config(tmp_path), ["web", "documents"])

    names = _tool_names(agent)

    assert names.index("get_web_content") < names.index("read_document")


def test_loaded_toolsets_in_strips_the_name_as_the_load_does():
    from kokua.core.build import loaded_toolsets_in

    assert loaded_toolsets_in(_load_exchange(" web ")) == ["web"]


# --- available = "all": the expansion is what every agent is built from --------------------------------


def _write_digest_skill(config) -> None:
    skill = config.skills_dir / "weekly-digest"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: weekly-digest\ndescription: Digest the week.\n---\n\nDo the thing.\n", encoding="utf-8"
    )


def _all_worker_config(tmp_path, **explorer):
    agents = {
        "assistant": AgentConfig(available=["time"], delegates_to=["explorer"]),
        "explorer": AgentConfig(description="Explores.", available=ALL_TOOLSETS, resident=["time"], **explorer),
    }
    config = _config(tmp_path, agents=agents, default_confirm=[], default_review=[])
    _write_digest_skill(config)
    return config


async def test_an_all_worker_spec_holds_its_resident_tools_and_loads_the_rest(tmp_path):
    from aimu import aio

    from kokua.core.agents import available_for

    config = _all_worker_config(tmp_path)
    spec = _specs(config)["explorer"]
    expanded = available_for(config, build_registry(config), "explorer")

    assert [fn.__name__ for fn in spec["tools"]] == ["get_current_date_and_time", "convert_time"]
    groups = spec["tool_groups"]
    assert [group.name for group in groups] == ["time", *[name for name in expanded if name != "time"]]
    assert groups[0].resident and not any(group.resident for group in groups[1:])
    assert "skills" not in [group.name for group in groups]
    worker = aio.Agent(MockAsyncModelClient([]), tools=spec["tools"], tool_groups=groups)
    assert {"get_current_date_and_time", "convert_time"} | LOADER_TOOLS == {
        fn.__name__ for fn in worker._effective_tools(None)
    }


async def test_an_all_entry_agent_holds_skills_and_lists_no_skill_twice(tmp_path):
    from kokua.core.build import entry_agent_system_message

    agents = {"assistant": AgentConfig(available=ALL_TOOLSETS, resident=["time", "skills"])}
    config = _config(tmp_path, agents=agents, default_confirm=[], default_review=[])
    _write_digest_skill(config)
    agent, state = _wired(config)

    names_line = entry_agent_system_message(config, state).split("Also available, load before use:")[1]
    assert "weekly-digest" not in names_line
    assert "author_skill" in _tool_names(agent)
    assert state.skill_manager is state.all_skills


async def test_all_grants_no_core_tool_and_never_reads_as_lean(tmp_path):
    from kokua.core.agents import DELEGATION_GUIDANCE, LEAN_DELEGATION_GUIDANCE

    alone = _specs(_all_worker_config(tmp_path / "alone"))["explorer"]
    assert "spawn_subagent" not in [fn.__name__ for fn in alone["tools"]]

    config = _all_worker_config(tmp_path / "delegating", delegates_to=["helper"])
    config.agents["helper"] = AgentConfig(description="Helps.", available=["time"])
    message = _specs(config)["explorer"]["system_message"]
    assert DELEGATION_GUIDANCE in message
    assert LEAN_DELEGATION_GUIDANCE not in message


# --- a skill written mid-session reaches "all" workers on their next spawn ----------------------------


def _authoring_all_worker_config(tmp_path):
    agents = {
        "assistant": AgentConfig(available=["mcp", "skills", "time"], delegates_to=["explorer", "clock"]),
        "explorer": AgentConfig(description="Explores.", available=ALL_TOOLSETS, resident=["time"]),
        "clock": AgentConfig(description="Tells the time.", available=["time"]),
    }
    return _config(tmp_path, agents=agents, default_confirm=[], default_review=[])


def _group_names(spec) -> list[str]:
    return [group.name for group in spec["tool_groups"]]


def _entry_tool(assistant, name):
    return next(fn for fn in assistant._agent.tools if getattr(fn, "__name__", "") == name)


async def test_a_skill_written_mid_session_reaches_an_all_worker_next_spawn(tmp_path, monkeypatch):
    import kokua.core.agents as agents_mod

    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    config = _authoring_all_worker_config(tmp_path)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    clock_before = _group_names(captured[-1]["clock"])
    assert "weekly-digest" not in _group_names(captured[-1]["explorer"])

    result = await _entry_tool(assistant, "author_skill")(
        name="weekly-digest", description="Digest the week.", body="Do the thing."
    )

    assert result.startswith("Created skill 'weekly-digest'")
    assert "weekly-digest" in _group_names(captured[-1]["explorer"])
    assert _group_names(captured[-1]["clock"]) == clock_before
    assistant._state.close()


async def test_a_server_added_at_runtime_does_not_reach_an_all_worker(tmp_path, monkeypatch):
    """The registry's MCP entries come from the config read at startup, so a skill refresh keeps them as
    they were: a server added since waits for a restart, while a skill written after it does not."""
    import kokua.core.agents as agents_mod

    async def fake_connect(url, **kwargs):
        return _FakeMCP([_fake_mcp_tool("get_quote")]), "none"

    monkeypatch.setattr("kokua.mcp.servers.connect_mcp", fake_connect)
    captured: list = []
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", _capturing_subagent_factory(captured))
    config = _authoring_all_worker_config(tmp_path)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    await _entry_tool(assistant, "add_mcp_server")(url="https://broker.example/mcp")
    assert "broker-example" not in _group_names(captured[-1]["explorer"])

    await _entry_tool(assistant, "author_skill")(name="weekly-digest", description="Digest the week.", body="Do it.")

    assert "weekly-digest" in _group_names(captured[-1]["explorer"])
    assert "broker-example" not in _group_names(captured[-1]["explorer"])
    assistant._state.close()


def test_a_skill_refresh_grows_the_approval_policies_before_any_worker_is_rebuilt(tmp_path):
    """A rebuilt delegate hands its workers the new scripts, so every gate naming a skill must have seen
    them first. A prose edit changes no tool, so it reports no build."""
    from kokua.core.build import refresh_skills
    from tests.core.test_skill_toolsets import write_skill

    config = _authoring_all_worker_config(tmp_path)
    write_skill(config, "weekly-digest", "Digest the week.", script="print(1)\n")
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config))
    events: list = []
    state.toolset_built = lambda toolset, names: events.append((toolset, names))
    state.refresh_workers = lambda agent: events.append("workers rebuilt")
    state.for_each_agent = lambda fn: fn(object())
    try:
        refresh_skills(state, tools_changed=True)
        assert events == [("weekly-digest", frozenset({"weekly_digest__run"})), "workers rebuilt"]

        events.clear()
        refresh_skills(state, tools_changed=False)
        assert events == ["workers rebuilt"]
    finally:
        state.close()


def test_a_delegate_that_fails_to_build_leaves_the_old_one_in_place(monkeypatch):
    """Removing the old delegate first would leave the agent unable to delegate at all until the next
    successful rebuild, for a failure that may have nothing to do with delegation."""
    from types import SimpleNamespace

    import kokua.core.agents as agents_mod
    from kokua.core.build import rebuild_delegation_tool

    def spawn_subagent():
        pass

    def broken(agent, config, state):
        raise RuntimeError("spec build failed")

    monkeypatch.setattr(agents_mod, "make_delegation_tool", broken)
    agent = SimpleNamespace(tools=[spawn_subagent])
    with pytest.raises(RuntimeError, match="spec build failed"):
        rebuild_delegation_tool(agent, SimpleNamespace(config=None))
    assert agent.tools == [spawn_subagent]


async def test_a_worker_rebuild_that_raises_reaches_the_model_after_the_write(tmp_path):
    config = _authoring_all_worker_config(tmp_path)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    def broken(agent):
        raise RuntimeError("spec build failed")

    assistant._state.refresh_workers = broken
    try:
        result = await _entry_tool(assistant, "author_skill")(
            name="weekly-digest", description="Digest the week.", body="Do the thing."
        )
        assert result.startswith("Created skill 'weekly-digest'")
        assert "The write happened, but workers were not given the change: spec build failed" in result
    finally:
        assistant._state.close()
