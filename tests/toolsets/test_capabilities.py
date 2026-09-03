"""Reading the capability registry, and composing a worker from what it holds."""

import pytest
from aimu.tools import tool as aimu_tool
from aimu.tools.builtin import SUBAGENT_SPEC_KEYS

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.toolsets.capabilities import (
    DEFAULT_SUBAGENT_NAME,
    TOOLSET,
    _catalogue,
    _compose_spec,
    make_capability_tools,
)
from kokua.registry.context import LiveState, ToolsetContext
from kokua.registry.registry import Toolset, ToolsetError, register


def _toolset(name: str, *, tools=(), **kwargs) -> Toolset:
    return Toolset(name=name, description=f"{name} description", build=lambda ctx: list(tools), **kwargs)


def _state(tmp_path, *, sources=None, **config_kwargs) -> LiveState:
    """A LiveState over a small registry, with one agent so `select` has an entry point to compare to."""
    config = AssistantConfig(
        data_dir=tmp_path,
        agents={"assistant": AgentConfig(tools=["capabilities"])},
        **config_kwargs,
    )
    sources = sources if sources is not None else [("AIMU capability", [_toolset("web"), _toolset("fs")])]
    return LiveState(config=config, registry=register(sources))


def _tools_by_name(state) -> dict:
    return {
        fn.__name__: fn
        for fn in make_capability_tools(ToolsetContext(state=state, agent=object(), agent_name="assistant"))
    }


@aimu_tool
async def _sample_tool() -> str:
    """A tool a test toolset can hand to a composed worker."""
    return "ok"


def _spec(state, *, name="quote-checker", tools=("web",), instructions="Check the quote.", extra_tools=None):
    return _compose_spec(name, list(tools), instructions, state, extra_tools=extra_tools, model="ollama:test")


class _RecordingSpawn:
    """Stands in for AIMU's spawn-tool factory, which builds a real model client per call.

    The dispatch path cannot run against MockAsyncModelClient because `make_async_subagent_tool`
    constructs its own client per spawn, so what is worth asserting here is the call it would make.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, model, **kwargs):
        self.calls.append({"model": model, **kwargs})

        async def spawn_subagent(agent_type: str, task: str) -> str:
            return f"{agent_type} ran: {task}"

        return spawn_subagent


def _recording(monkeypatch) -> _RecordingSpawn:
    """Patches the factory where it is defined, since compose_subagent imports it at call time.

    That import is deliberately function-level, to keep the AIMU surface the startup preflight probes
    off the module's import path, so there is no module attribute here to replace.
    """
    spawn = _RecordingSpawn()
    monkeypatch.setattr("aimu.aio.tools.builtin.make_async_subagent_tool", spawn)
    return spawn


def _compose(state, monkeypatch):
    spawn = _recording(monkeypatch)
    return _tools_by_name(state)["compose_subagent"], spawn


async def test_list_capabilities_reports_every_name_with_its_provider_and_description(tmp_path):
    listing = await _tools_by_name(_state(tmp_path))["list_capabilities"]()
    assert "web [AIMU capability]: web description" in listing
    assert "fs [AIMU capability]: fs description" in listing


async def test_list_capabilities_sorts_by_name(tmp_path):
    listing = await _tools_by_name(_state(tmp_path))["list_capabilities"]()
    assert listing.index("fs [") < listing.index("web [")


async def test_list_capabilities_filters_on_name_and_description(tmp_path):
    sources = [("MCP server", [Toolset(name="stocks", description="Quotes and trades.", build=lambda ctx: [])])]
    sources.append(("AIMU capability", [_toolset("web")]))
    listing = await _tools_by_name(_state(tmp_path, sources=sources))["list_capabilities"](filter="quotes")
    assert "stocks" in listing
    assert "web" not in listing


async def test_list_capabilities_says_so_when_nothing_matches(tmp_path):
    listing = await _tools_by_name(_state(tmp_path))["list_capabilities"](filter="zzz")
    assert "zzz" in listing
    assert "No capability" in listing


async def test_list_capabilities_omits_its_own_entry(tmp_path):
    """The agent reading the catalogue already holds this toolset; listing it back is noise, and it
    would invite naming it to compose_subagent, which only earns a rejection."""
    sources = [("core", [_toolset("capabilities")]), ("AIMU capability", [_toolset("web")])]
    listing = await _tools_by_name(_state(tmp_path, sources=sources))["list_capabilities"]()
    assert "capabilities [" not in listing
    assert "web [" in listing


async def test_list_capabilities_never_builds_a_toolset(tmp_path):
    """Discovery must not pay build()'s side effects to answer a question: `memory` instantiates a
    store and loads an embedding model, and a plugin's build may fail outright."""
    built = []

    def _explode(ctx):
        built.append(True)
        raise AssertionError("build() must not run during discovery")

    sources = [("plugin", [Toolset(name="boom", description="Never built.", build=_explode)])]
    await _tools_by_name(_state(tmp_path, sources=sources))["list_capabilities"]()
    assert built == []


def test_the_toolset_is_cross_cutting_and_not_entry_point_only():
    """Cross-cutting so a lean supervisor declaring it still reads as lean to the delegation guidance;
    not entry-point-only because a composed worker holding it is how recursion works."""
    assert TOOLSET.cross_cutting
    assert not TOOLSET.entry_point_only


def test_the_toolset_declares_a_hot_max_depth_setting():
    settings = {setting.key: setting for setting in TOOLSET.settings}
    assert settings["max_depth"].kind is int
    assert settings["max_depth"].default == 3
    assert settings["max_depth"].hot


def test_compose_spec_carries_only_keys_aimu_accepts(tmp_path):
    """SUBAGENT_SPEC_KEYS is the published set AIMU validates a spec against, and the same symbol the
    aimu_compat preflight probe grips. Asserting against it means a spec-shape drift fails here."""
    spec = _spec(_state(tmp_path))
    assert set(spec) <= SUBAGENT_SPEC_KEYS


def test_compose_spec_builds_the_named_toolsets_tools(tmp_path):
    sources = [("AIMU capability", [_toolset("web", tools=[_sample_tool])])]
    spec = _spec(_state(tmp_path, sources=sources))
    assert [fn.__name__ for fn in spec["tools"]] == ["_sample_tool"]


def test_compose_spec_appends_each_toolsets_guidance_to_the_instructions(tmp_path):
    """A capability's usage instructions travel with it into an ad-hoc worker exactly as they do into a
    declared one, so installing a toolset brings the words that make the model use it."""
    guided = Toolset(name="web", description="Web.", build=lambda ctx: [], guidance=" Search before answering.")
    spec = _spec(_state(tmp_path, sources=[("AIMU capability", [guided])]))
    assert spec["system_message"] == "Check the quote. Search before answering."


def test_compose_spec_falls_back_to_default_instructions_when_given_none(tmp_path):
    spec = _spec(_state(tmp_path), instructions="   ")
    assert spec["system_message"].strip()


def test_compose_spec_rejects_an_unknown_capability_by_name(tmp_path):
    with pytest.raises(ToolsetError) as excinfo:
        _spec(_state(tmp_path), tools=["nope"])
    assert "nope" in str(excinfo.value)
    assert "Available toolsets" in str(excinfo.value)


def test_compose_spec_rejects_an_entry_point_only_capability(tmp_path):
    sources = [("AIMU capability", [_toolset("skills", entry_point_only=True)])]
    with pytest.raises(ToolsetError):
        _spec(_state(tmp_path, sources=sources), tools=["skills"])


def test_compose_spec_rejects_an_entry_point_only_capability_even_when_named_after_the_entry_agent(tmp_path):
    """`select` rejects an entry-point-only toolset by testing `agent != entry_point`, so a worker the
    model happened to call "assistant" would otherwise resolve `skills` and then reach `build` with
    agent=None, where make_skill_script_tool needs a live agent."""
    sources = [("AIMU capability", [_toolset("skills", entry_point_only=True)])]
    with pytest.raises(ToolsetError):
        _spec(_state(tmp_path, sources=sources), name="assistant", tools=["skills"])


def test_compose_spec_rejects_naming_this_toolset_itself(tmp_path):
    """Naming `capabilities` among `tools` would let a worker rebuild a fresh, full-budget
    `compose_subagent` of its own, bypassing the depth count the caller already holds; the rejection is
    checked ahead of `select`, so it fires whether or not `capabilities` is even installed here."""
    with pytest.raises(ToolsetError) as excinfo:
        _spec(_state(tmp_path), tools=["capabilities"])
    assert "max_depth" in str(excinfo.value)


@pytest.mark.parametrize("variant", ["Capabilities", "CAPABILITIES", " capabilities "])
def test_compose_spec_rejects_naming_this_toolset_however_it_is_spelled(tmp_path, variant):
    """An exact-match guard would let a variant spelling fall through to `select`, which answers an
    unresolvable name by listing the available toolsets -- `capabilities` among them, re-advertising the
    one name the guard exists to close."""
    with pytest.raises(ToolsetError) as excinfo:
        _spec(_state(tmp_path), tools=[variant])
    assert "max_depth" in str(excinfo.value)


def test_compose_spec_inherits_the_assistant_thinking_default(tmp_path):
    """A composed worker resolves its tuning through the same `thinking_for` call an undeclared worker
    does: the name is absent from [agents.*], so the [assistant] default applies with no special case."""
    spec = _spec(_state(tmp_path, thinking="high"))
    assert spec["thinking"] == "high"


def test_compose_spec_omits_generate_kwargs_when_nothing_is_configured(tmp_path):
    """A key absent from the file must be absent from the request, so a model card's own tuned profile
    stays in force."""
    assert "generate_kwargs" not in _spec(_state(tmp_path))


def test_compose_spec_carries_the_resolved_model(tmp_path):
    assert _spec(_state(tmp_path))["model"] == "ollama:test"


def test_compose_spec_hands_the_worker_a_composition_tool_when_given_one(tmp_path):
    """Whether a worker may compose again is the caller's decision, made from the depth count it holds;
    this function only places what it is handed."""
    spec = _spec(_state(tmp_path), extra_tools=[_sample_tool])
    assert "_sample_tool" in {fn.__name__ for fn in spec["tools"]}


def test_compose_spec_leaves_the_worker_without_one_by_default(tmp_path):
    assert _spec(_state(tmp_path))["tools"] == []


def test_compose_spec_inherits_the_assistant_generation_defaults(tmp_path):
    """The omission case alone would pass for an implementation that never wrote the key at all, so the
    inheritance itself is pinned: a composed worker reads [assistant.generation] the way any agent
    absent from [agents.*] does."""
    spec = _spec(_state(tmp_path, generation={"temperature": 0.2}))
    assert spec["generate_kwargs"] == {"temperature": 0.2}


def test_the_catalogue_labels_an_entry_whose_provider_is_unrecorded(tmp_path):
    """`LiveState.registry` is typed as a plain dict, so a state assembled by hand carries no provider
    map; a missing label must degrade to an unlabeled line rather than break discovery."""
    assert _catalogue({"web": _toolset("web")}, "") == "web [unknown]: web description"


async def test_compose_subagent_runs_the_worker_under_its_given_name(tmp_path, monkeypatch):
    compose, spawn = _compose(_state(tmp_path), monkeypatch)
    result = await compose("quote-checker", "Check AAPL.", ["web"], "Check quotes.")
    assert result == "quote-checker ran: Check AAPL."
    assert list(spawn.calls[0]["agent_types"]) == ["quote-checker"]


async def test_compose_subagent_labels_an_unnamed_worker_rather_than_leaving_the_card_blank(tmp_path, monkeypatch):
    """The agent_types key is both the observer's card label and AIMU's Agent name, so it reaches the
    web sub-agent card and the logs; a blank one makes two concurrent workers indistinguishable."""
    compose, spawn = _compose(_state(tmp_path), monkeypatch)
    await compose("  ", "Check AAPL.", ["web"], "Check quotes.")
    assert list(spawn.calls[0]["agent_types"]) == [DEFAULT_SUBAGENT_NAME]


async def test_compose_subagent_forwards_the_approval_gate_and_the_observer(tmp_path, monkeypatch):
    """Every tool a composed worker holds must still route to the human through confirm_tools, and the
    run must still reach the web sub-agent card."""
    state = _state(tmp_path)
    state.tool_approval = lambda *a, **k: True
    state.observer = object()
    compose, spawn = _compose(state, monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    assert spawn.calls[0]["tool_approval"] is state.tool_approval
    assert spawn.calls[0]["observer"] is state.observer


async def test_compose_subagent_calls_aimu_with_max_depth_one_at_every_level(tmp_path, monkeypatch):
    """Kokua owns the recursion, exactly as `build_agent_specs` does: AIMU's own max_depth would give
    every level the same menu, which cannot express a worker composed fresh per call."""
    compose, spawn = _compose(_state(tmp_path), monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    assert spawn.calls[0]["max_depth"] == 1


async def test_compose_subagent_passes_the_configured_tool_loop_cap(tmp_path, monkeypatch):
    """A composed worker is built per call and discarded with it, so `[assistant].max_iterations` is the
    only tier it has, and this is the one construction site nothing else watches. Dropping the argument
    would leave every composed worker on AIMU's own default instead of the configured one, silently.
    """
    state = _state(tmp_path, max_iterations=30)
    compose, spawn = _compose(state, monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    assert spawn.calls[0]["max_iterations"] == 30


async def test_a_composed_worker_can_compose_again_while_depth_remains(tmp_path, monkeypatch):
    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 2}})
    compose, spawn = _compose(state, monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    worker_tools = spawn.calls[0]["agent_types"]["w"]["tools"]
    assert "compose_subagent" in {fn.__name__ for fn in worker_tools}


async def test_a_worker_that_can_compose_again_can_also_look_names_up(tmp_path, monkeypatch):
    """A nested compose_subagent is useless without list_capabilities to find names with, so the two are
    handed to a worker as a pair rather than compose_subagent alone."""
    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 2}})
    compose, spawn = _compose(state, monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    worker_tools = spawn.calls[0]["agent_types"]["w"]["tools"]
    assert "list_capabilities" in {fn.__name__ for fn in worker_tools}


async def test_the_last_worker_in_the_chain_gets_no_composition_tool(tmp_path, monkeypatch):
    """The decrementing counter is the entire termination argument: at zero the worker gets neither
    list_capabilities nor compose_subagent, since discovery with no way to act on what it finds is
    useless to the worker holding it."""
    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 1}})
    compose, spawn = _compose(state, monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    worker_tools = spawn.calls[0]["agent_types"]["w"]["tools"]
    assert "compose_subagent" not in {fn.__name__ for fn in worker_tools}


async def test_the_decrement_reaches_a_second_level_of_composition(tmp_path, monkeypatch):
    """The presence tests above only look one level deep, so writing `remaining_depth=depth` instead of
    `depth - 1` (an infinite chain) would still pass every one of them. Driving the nested tool a second
    worker deep is what actually pins the decrement, which is the whole termination argument."""
    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 3}})
    compose, spawn = _compose(state, monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    first_tools = {fn.__name__: fn for fn in spawn.calls[0]["agent_types"]["w"]["tools"]}
    await first_tools["compose_subagent"]("w2", "Do it.", ["web"], "Instructions.")
    second_tools = spawn.calls[1]["agent_types"]["w2"]["tools"]
    assert "compose_subagent" in {fn.__name__ for fn in second_tools}

    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 2}})
    compose, spawn = _compose(state, monkeypatch)
    await compose("w", "Do it.", ["web"], "Instructions.")
    first_tools = {fn.__name__: fn for fn in spawn.calls[0]["agent_types"]["w"]["tools"]}
    await first_tools["compose_subagent"]("w2", "Do it.", ["web"], "Instructions.")
    second_tools = spawn.calls[1]["agent_types"]["w2"]["tools"]
    assert "compose_subagent" not in {fn.__name__ for fn in second_tools}


async def test_the_depth_cap_is_read_at_call_time_so_a_hot_change_reaches_a_built_agent(tmp_path, monkeypatch):
    """`build` runs once per conversation agent, so a build-time read would strand a hot setting change
    until restart, which is what hot=True promises will not happen."""
    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 2}})
    compose, spawn = _compose(state, monkeypatch)
    state.config.toolset_settings["capabilities"]["max_depth"] = 1
    await compose("w", "Do it.", ["web"], "Instructions.")
    worker_tools = spawn.calls[0]["agent_types"]["w"]["tools"]
    assert "compose_subagent" not in {fn.__name__ for fn in worker_tools}


async def test_a_max_depth_of_zero_switches_composition_off(tmp_path, monkeypatch):
    """The cap is a genuine off switch, not just a nesting bound: at zero the tool is still present but
    composes nothing, so a user who wants the behavior gone can turn it off without editing an agent."""
    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 0}})
    compose, spawn = _compose(state, monkeypatch)
    result = await compose("w", "Do it.", ["web"], "Instructions.")
    assert "max_depth" in result
    assert spawn.calls == []


async def test_compose_subagent_refuses_to_hand_a_worker_its_own_toolset_by_name(tmp_path, monkeypatch):
    """Naming `capabilities` in `tools` is the escape hatch that would defeat the depth cap: a worker
    handed that name would resolve a fresh, full-budget compose_subagent of its own rather than the
    depth-limited one this call would otherwise build, regardless of what the cap says."""
    state = _state(tmp_path, toolset_settings={"capabilities": {"max_depth": 1}})
    compose, spawn = _compose(state, monkeypatch)
    result = await compose("w", "Do it.", ["capabilities"], "Instructions.")
    assert "max_depth" in result
    assert spawn.calls == []


async def test_compose_subagent_reports_an_unknown_capability_as_text(tmp_path, monkeypatch):
    """A tool that raises breaks the agent's tool loop, so an unresolvable name comes back as an answer
    the model can act on."""
    compose, spawn = _compose(_state(tmp_path), monkeypatch)
    result = await compose("w", "Do it.", ["nope"], "Instructions.")
    assert "nope" in result
    assert "Available toolsets" in result
    assert spawn.calls == []


async def test_compose_subagent_asks_for_a_capability_when_given_none(tmp_path, monkeypatch):
    compose, spawn = _compose(_state(tmp_path), monkeypatch)
    result = await compose("w", "Do it.", [], "Instructions.")
    assert "list_capabilities" in result
    assert spawn.calls == []


async def test_compose_subagent_uses_the_resolved_default_not_the_holders_client(tmp_path, monkeypatch):
    """With no [assistant].model, a composition runs on the string AIMU's default resolved to.

    It used to read the holder's already-built client instead. A client reports a resolved ``Model``
    enum, so an ``@base_url`` in the default never survived the trip, and the composed sub-agent was
    built against the provider default while the agent that composed it talked to the override.
    """
    from tests.conftest import TEST_DEFAULT_MODEL

    class _Agent:
        model_client = type("_Client", (), {"model": "ollama:resolved"})()

    spawn = _recording(monkeypatch)
    state = _state(tmp_path)
    tools = {
        fn.__name__: fn
        for fn in make_capability_tools(ToolsetContext(state=state, agent=_Agent(), agent_name="assistant"))
    }
    await tools["compose_subagent"]("w", "Do it.", ["web"], "Instructions.")
    assert spawn.calls[0]["model"] == TEST_DEFAULT_MODEL


def test_the_guidance_ranks_the_declared_roles_above_composing_one(tmp_path):
    """Composition is the fallback, not the first move: spawn_subagent's roles carry instructions
    written for their job, and an agent that composes by default pays an extra step for a worse worker."""
    assert "spawn_subagent" in TOOLSET.guidance
    assert "list_capabilities" in TOOLSET.guidance
    assert "compose_subagent" in TOOLSET.guidance


def test_a_composed_sub_agents_toolsets_are_built_under_its_own_label(tmp_path):
    """The ad-hoc label is what `ctx.agent_name` carries here, and it is the honest answer: the label is
    in no `[agents.*]` table, so every per-agent accessor falls back to the `[assistant]` defaults, which
    is exactly the tier a composed sub-agent runs on."""
    holders: list[str] = []

    def record(ctx) -> list:
        holders.append(ctx.agent_name)
        return []

    state = _state(tmp_path, sources=[("test", [Toolset(name="probe", description="records", build=record)])])

    _spec(state, name="quote-checker", tools=("probe",))

    assert holders == ["quote-checker"]
