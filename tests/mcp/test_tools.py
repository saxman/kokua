"""The runtime add/remove MCP agent tools."""

from aimu import aio

from kokua.mcp.tools import make_mcp_tools


async def _noop_notify(message: str) -> None:
    return None


def _tools(tmp_path, connections=None):
    return make_mcp_tools(
        lambda fn: None,
        connections if connections is not None else [],
        notify=_noop_notify,
        oauth_storage_dir=tmp_path / "oauth",
        config_path=tmp_path / "config.toml",
    )


async def test_add_mcp_server_tool_returns_bearer_instruction(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise Exception("401 Unauthorized")
        raise Exception("Registration failed: 404")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    add_mcp_server, _ = _tools(tmp_path)
    msg = await add_mcp_server(url="https://git.example/mcp")
    assert "bearer token" in msg.lower()
    assert "git.example" in msg


async def test_connect_is_reached_through_the_servers_module(monkeypatch, tmp_path):
    """The tools call ``servers.connect_mcp`` by attribute, not by an import-time-bound name, so one
    patch point covers both the boot reconnect and the runtime add. Binding the name here would split
    that seam and let a test patch only half the paths."""
    from kokua.mcp import servers

    async def fake_connect(url, **kw):
        raise Exception(f"refused {url}")

    monkeypatch.setattr(servers, "connect_mcp", fake_connect)

    add_mcp_server, _ = _tools(tmp_path)
    assert "refused https://svc/mcp" in await add_mcp_server(url="https://svc/mcp")
