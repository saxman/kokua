"""A configured MCP server is a toolset like any other, resolved against the live connections."""

import pytest

from kokua.config.file import ConfigError
from kokua.config.schema import AgentConfig, AssistantConfig, MCPServerConfig
from kokua.toolsets.agents import build_registry
from kokua.toolsets.context import LiveState, ToolsetContext
from tests.helpers import core_table


class FakeConnection:
    def __init__(self, url, tools):
        self.url = url
        self.callables = tools


def _config(**kwargs) -> AssistantConfig:
    return AssistantConfig(
        agents={"assistant": AgentConfig(tools=["stocks"])},
        entry_agent="assistant",
        load_plugins=False,
        mcp_servers=[MCPServerConfig(url="https://broker.example.com/mcp", name="stocks")],
        **kwargs,
    )


def test_a_configured_server_is_registered_under_its_name():
    assert "stocks" in build_registry(_config())


def test_a_servers_toolset_builds_its_live_tools(tmp_path):
    def quote():
        pass

    config = _config(data_dir=tmp_path)
    registry = build_registry(config)
    state = LiveState(
        config=config,
        connections=[FakeConnection("https://broker.example.com/mcp", [quote])],
        registry=registry,
    )
    tools = registry["stocks"].build(ToolsetContext(state=state, agent=None))
    assert tools == [quote]


def test_a_configured_but_unconnected_server_builds_nothing(tmp_path):
    config = _config(data_dir=tmp_path)
    registry = build_registry(config)
    state = LiveState(config=config, connections=[], registry=registry)
    assert registry["stocks"].build(ToolsetContext(state=state, agent=None)) == []


def test_a_server_toolset_is_not_cross_cutting():
    assert build_registry(_config())["stocks"].cross_cutting is False


def test_a_server_whose_name_collides_with_a_core_toolset_is_rejected():
    config = AssistantConfig(
        agents={"assistant": AgentConfig(tools=["memory"])},
        entry_agent="assistant",
        load_plugins=False,
        mcp_servers=[MCPServerConfig(url="https://example.com/mcp", name="memory")],
    )
    from kokua.toolsets.registry import ToolsetError

    with pytest.raises(ToolsetError) as excinfo:
        build_registry(config)
    message = str(excinfo.value)
    assert "memory" in message
    assert "AIMU capability" in message
    assert "MCP server" in message


def test_two_servers_colliding_under_the_same_provider_label_name_their_urls():
    """Two MCP servers claiming one name collide under the identical 'MCP server' label on both sides,
    so the label alone can't tell them apart; each toolset's description (which for a server is its URL)
    is what still lets a user find both entries in config.toml."""
    config = AssistantConfig(
        agents={"assistant": AgentConfig(tools=["shared"])},
        entry_agent="assistant",
        load_plugins=False,
        mcp_servers=[
            MCPServerConfig(url="https://one.example.com/mcp", name="shared"),
            MCPServerConfig(url="https://two.example.com/mcp", name="shared"),
        ],
    )
    from kokua.toolsets.registry import ToolsetError

    with pytest.raises(ToolsetError) as excinfo:
        build_registry(config)
    message = str(excinfo.value)
    assert "shared" in message
    assert "https://one.example.com/mcp" in message
    assert "https://two.example.com/mcp" in message


def test_a_server_without_a_name_is_a_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[assistant]\nagent = "assistant"\n\n[agents.assistant]\ntools = ["time"]\n\n'
        '[[mcp.server]]\nurl = "https://example.com/mcp"\n'
    )
    from kokua.config.file import load

    with pytest.raises(ConfigError) as excinfo:
        AssistantConfig(**load(str(path), table=core_table()))
    assert "name" in str(excinfo.value)
