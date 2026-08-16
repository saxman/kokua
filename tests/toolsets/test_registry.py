"""The registry's three jobs: collision detection, name resolution, and tool assembly."""

import pytest

from kokua.toolsets.registry import Toolset, ToolsetError, build_tools, register, select


def _toolset(name: str, *, tools=(), **kwargs) -> Toolset:
    return Toolset(name=name, description=f"{name} description", build=lambda ctx: list(tools), **kwargs)


def test_register_indexes_by_name():
    registry = register([("core subsystem", [_toolset("memory"), _toolset("config")])])
    assert sorted(registry) == ["config", "memory"]


def test_register_rejects_a_name_claimed_twice_and_names_both_providers():
    with pytest.raises(ToolsetError) as excinfo:
        register([("core subsystem", [_toolset("stocks")]), ("MCP server", [_toolset("stocks")])])
    message = str(excinfo.value)
    assert "stocks" in message
    assert "core subsystem" in message
    assert "MCP server" in message


def test_register_collision_message_reads_cleanly_when_a_description_ends_in_a_period():
    """Every MCP toolset's description already ends in a period ("Tools from the MCP server at {url}.");
    interpolating it straight into the template's own closing sentence used to render "...mcp.)." --
    a closing paren immediately followed by the template's period. Covers both a same-provider collision
    (two MCP servers deriving one name) and a different-provider one, since either could double up."""
    mcp_one = Toolset(
        name="stocks", description="Tools from the MCP server at https://broker/quotes.", build=lambda ctx: []
    )
    mcp_two = Toolset(
        name="stocks", description="Tools from the MCP server at https://broker/orders.", build=lambda ctx: []
    )
    with pytest.raises(ToolsetError) as excinfo:
        register([("MCP server", [mcp_one]), ("MCP server", [mcp_two])])
    message = str(excinfo.value)
    assert ").)" not in message
    assert ".)." not in message

    with pytest.raises(ToolsetError) as excinfo:
        register([("core subsystem", [_toolset("stocks")]), ("MCP server", [mcp_one])])
    message = str(excinfo.value)
    assert ").)" not in message
    assert ".)." not in message


def test_select_returns_toolsets_in_declared_order():
    registry = register([("core subsystem", [_toolset("a"), _toolset("b")])])
    selected = select(["b", "a"], registry, agent="assistant", entry_point="assistant")
    assert [t.name for t in selected] == ["b", "a"]


def test_select_deduplicates_a_repeated_name():
    registry = register([("core subsystem", [_toolset("a")])])
    selected = select(["a", "a"], registry, agent="assistant", entry_point="assistant")
    assert [t.name for t in selected] == ["a"]


def test_select_rejects_an_unresolvable_name_and_lists_the_available_ones():
    registry = register([("core subsystem", [_toolset("memory"), _toolset("config")])])
    with pytest.raises(ToolsetError) as excinfo:
        select(["memry"], registry, agent="researcher", entry_point="assistant")
    message = str(excinfo.value)
    assert "memry" in message
    assert "researcher" in message
    assert "config, memory" in message


def test_select_rejects_an_entry_point_only_toolset_on_another_agent():
    registry = register([("core subsystem", [_toolset("skills", entry_point_only=True)])])
    with pytest.raises(ToolsetError) as excinfo:
        select(["skills"], registry, agent="researcher", entry_point="assistant")
    assert "skills" in str(excinfo.value)
    assert "researcher" in str(excinfo.value)


def test_select_allows_an_entry_point_only_toolset_on_the_entry_point():
    registry = register([("core subsystem", [_toolset("skills", entry_point_only=True)])])
    selected = select(["skills"], registry, agent="assistant", entry_point="assistant")
    assert [t.name for t in selected] == ["skills"]


def test_build_tools_concatenates_and_keeps_the_first_tool_of_a_repeated_name():
    def first():
        pass

    def second():
        pass

    second.__name__ = "first"
    tools = build_tools([_toolset("a", tools=[first]), _toolset("b", tools=[second])], ctx=None)
    assert tools == [first]


def test_plugins_module_reexports_the_public_contract():
    from kokua import plugins

    assert plugins.Toolset is Toolset
    assert plugins.TOOLSET_GROUP == "kokua.toolsets"
    assert hasattr(plugins, "ToolsetContext")
    assert hasattr(plugins, "discover_toolsets")
