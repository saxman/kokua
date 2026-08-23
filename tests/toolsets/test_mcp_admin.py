"""The ``mcp-admin`` toolset: how a runtime add/remove is reported back to the model."""

from aimu import aio

from kokua.toolsets.mcp_admin import make_mcp_tools
from kokua.mcp.auth import OAuthSettings


async def _noop_notify(message: str) -> None:
    return None


def _tools(tmp_path, connections=None):
    return make_mcp_tools(
        lambda fn: None,
        connections if connections is not None else [],
        notify=_noop_notify,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
        config_path=tmp_path / "config.toml",
    )


async def test_add_relays_the_bearer_token_instruction(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise Exception("401 Unauthorized")
        raise Exception("Registration failed: 404")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    add_mcp_server, _ = _tools(tmp_path)
    message = await add_mcp_server(url="https://git.example/mcp")

    assert "bearer token" in message.lower()
    assert "git.example" in message


async def test_add_reports_a_connection_failure_with_its_cause(monkeypatch, tmp_path):
    from kokua.mcp import servers

    async def fake_connect(url, **kw):
        raise Exception(f"refused {url}")

    monkeypatch.setattr(servers, "connect_mcp", fake_connect)

    add_mcp_server, _ = _tools(tmp_path)

    assert "refused https://svc/mcp" in await add_mcp_server(url="https://svc/mcp")


async def test_add_reports_an_already_connected_server(tmp_path):
    from kokua.mcp.servers import ServerConnection

    connections = [ServerConnection(url="https://svc/mcp", client=None, tools=[], auth_mode="none")]
    add_mcp_server, _ = _tools(tmp_path, connections)

    message = await add_mcp_server(url="https://svc/mcp")

    assert "Already connected" in message and "remove_mcp_server" in message


async def test_add_says_a_new_server_reaches_nothing_until_a_restart(monkeypatch, tmp_path):
    """The load-bearing qualifier: the toolset registry is built once at startup, so a model that
    reads this must not promise the user it can use the server in this conversation."""

    class FakeClient:
        async def as_tools(self):
            return [_named("search_docs")]

    async def fake_connect(url, **kw):
        return FakeClient(), "none"

    monkeypatch.setattr("kokua.mcp.servers.connect_mcp", fake_connect)

    add_mcp_server, _ = _tools(tmp_path)
    message = await add_mcp_server(url="https://svc/mcp")

    assert "search_docs" in message
    assert "cannot call these yourself" in message.lower()
    assert "restart" in message.lower()


async def test_remove_reports_an_unconnected_server(tmp_path):
    _, remove_mcp_server = _tools(tmp_path)

    assert "No MCP server is connected" in await remove_mcp_server(url="https://svc/mcp")


async def test_remove_lists_only_the_names_it_actually_freed(tmp_path):
    """A same-named tool from another still-connected server is not reported as gone."""
    from kokua.mcp.servers import ServerConnection

    class FakeClient:
        async def aclose(self):
            return None

    going = ServerConnection(url="https://a/mcp", client=FakeClient(), tools=["shared", "only_here"], auth_mode="none")
    staying = ServerConnection(url="https://b/mcp", client=FakeClient(), tools=["shared"], auth_mode="none")
    _, remove_mcp_server = _tools(tmp_path, [going, staying])

    message = await remove_mcp_server(url="https://a/mcp")

    assert "only_here" in message and "shared" not in message
    assert "sub-agent you spawn from now on" in message


def _named(name):
    def fn():
        return None

    fn.__name__ = name
    return fn
