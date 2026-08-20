"""Reading the capability registry, and composing a worker from what it holds."""

import pytest
from aimu.tools import tool as aimu_tool
from aimu.tools.builtin import SUBAGENT_SPEC_KEYS

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.toolsets.capabilities import TOOLSET, _compose_spec, make_capability_tools
from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.registry import Toolset, ToolsetError, register


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
    return {fn.__name__: fn for fn in make_capability_tools(ToolsetContext(state=state, agent=object()))}


@aimu_tool
async def _sample_tool() -> str:
    """A tool a test toolset can hand to a composed worker."""
    return "ok"


def _spec(state, *, name="quote-checker", tools=("web",), instructions="Check the quote.", compose_tool=None):
    return _compose_spec(name, list(tools), instructions, state, compose_tool=compose_tool, model="ollama:test")


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
    spec = _spec(_state(tmp_path), compose_tool=_sample_tool)
    assert "_sample_tool" in {fn.__name__ for fn in spec["tools"]}


def test_compose_spec_leaves_the_worker_without_one_by_default(tmp_path):
    assert _spec(_state(tmp_path))["tools"] == []
