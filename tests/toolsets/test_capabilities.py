"""Reading the capability registry, and composing a worker from what it holds."""

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.toolsets.capabilities import TOOLSET, make_capability_tools
from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.registry import Toolset, register


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
