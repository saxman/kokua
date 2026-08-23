"""Unit tests for the auth-required heuristic that gates the runtime-add OAuth fallback."""

import pytest
from aimu import aio

from kokua.mcp import servers as mcp
from kokua.config import MCPServerConfig
from kokua.mcp.auth import OAuthSettings
from kokua.mcp.servers import (
    _looks_like_auth_required,
    _looks_like_registration_unsupported,
)


@pytest.mark.parametrize(
    "text",
    [
        "401 Unauthorized",
        "403 Forbidden",
        "server returned WWW-Authenticate: Bearer",
        "oauth required",
        # A server that signals its requirement with a 400 + plain message rather than a 401.
        "bad request: missing required Authorization header",
        "authentication required",
    ],
)
def test_auth_signals_trigger_oauth_fallback(text: str):
    assert _looks_like_auth_required(Exception(text))


@pytest.mark.parametrize(
    "text",
    [
        "Name or service not known",
        "Connection refused",
        "500 Internal Server Error",
    ],
)
def test_non_auth_failures_do_not_trigger_oauth(text: str):
    assert not _looks_like_auth_required(Exception(text))


def test_registration_failure_is_detected():
    assert _looks_like_registration_unsupported(Exception("Registration failed: 404 404 page not found"))


def test_ordinary_oauth_failure_is_not_registration():
    assert not _looks_like_registration_unsupported(Exception("token exchange failed: invalid_grant"))


async def test_connect_mcp_bearer_token_skips_oauth(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        assert auth == "tok"
        return "client"

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    client, mode = await mcp.connect_mcp(
        "https://svc/mcp", bearer_token="tok", notify=_noop_notify, oauth=OAuthSettings(storage_dir=tmp_path)
    )
    assert (client, mode) == ("client", "bearer")


async def test_connect_mcp_asks_for_bearer_when_registration_unsupported(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise Exception("401 Unauthorized")
        raise Exception("Registration failed: 404 404 page not found")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    with pytest.raises(mcp.BearerTokenRequired) as excinfo:
        await mcp.connect_mcp(
            "https://git.example/mcp", notify=_noop_notify, oauth=OAuthSettings(storage_dir=tmp_path / "oauth")
        )
    assert "git.example" in str(excinfo.value)
    assert "bearer token" in str(excinfo.value).lower()


async def test_connect_mcp_other_oauth_failure_reraises_unchanged(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise Exception("401 Unauthorized")
        raise RuntimeError("token exchange failed")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    with pytest.raises(RuntimeError, match="token exchange failed"):
        await mcp.connect_mcp(
            "https://svc/mcp", notify=_noop_notify, oauth=OAuthSettings(storage_dir=tmp_path / "oauth")
        )


def test_resolve_server_token_reads_env(monkeypatch):
    monkeypatch.setenv("MY_MCP_TOKEN", "secret")
    server = MCPServerConfig(url="https://svc/mcp", name="svc", token_env="MY_MCP_TOKEN")
    assert mcp._resolve_server_token(server) == "secret"


def test_resolve_server_token_none_without_token_env():
    assert mcp._resolve_server_token(MCPServerConfig(url="https://svc/mcp", name="svc")) is None


def test_resolve_server_token_warns_when_env_unset(monkeypatch, caplog):
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    server = MCPServerConfig(url="https://svc/mcp", name="svc", token_env="MISSING_MCP_TOKEN")
    with caplog.at_level("WARNING"):
        assert mcp._resolve_server_token(server) is None
    assert any("MISSING_MCP_TOKEN" in rec.message for rec in caplog.records)


async def _noop_notify(message: str) -> None:
    pass


# --- The runtime add/remove ------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, tool_names=()):
        self._tool_names = list(tool_names)
        self.closed = False

    async def as_tools(self):
        return [_named(name) for name in self._tool_names]

    async def aclose(self):
        self.closed = True


def _named(name):
    def fn():
        return None

    fn.__name__ = name
    return fn


async def _noop_notify(message: str) -> None:
    return None


def _kwargs(tmp_path, **overrides):
    base = {
        "notify": _noop_notify,
        "oauth": OAuthSettings(storage_dir=tmp_path / "oauth"),
        "config_path": tmp_path / "config.toml",
        "for_each_agent": lambda fn: None,
    }
    base.update(overrides)
    return base


def _remove_kwargs(tmp_path, **overrides):
    """remove_server needs no connect machinery, so it takes neither notify nor the OAuth dir."""
    base = {"config_path": tmp_path / "config.toml", "for_each_agent": lambda fn: None}
    base.update(overrides)
    return base


async def test_add_server_records_the_connection_and_persists_a_reconnectable_one(monkeypatch, tmp_path):
    async def fake_connect(url, **kw):
        return _FakeClient(["search_docs"]), "none"

    monkeypatch.setattr(mcp, "connect_mcp", fake_connect)
    connections = []

    result = await mcp.add_server(connections, "https://svc/mcp", **_kwargs(tmp_path))

    assert result.tool_names == ["search_docs"] and result.auth_mode == "none"
    assert result.persisted is True
    assert [conn.url for conn in connections] == ["https://svc/mcp"]
    assert "https://svc/mcp" in (tmp_path / "config.toml").read_text(encoding="utf-8")


async def test_add_server_keeps_a_bearer_server_out_of_the_config_file(monkeypatch, tmp_path):
    """Its secret is never written to disk, so it cannot be reconnected at boot and is not recorded."""

    async def fake_connect(url, **kw):
        return _FakeClient([]), "bearer"

    monkeypatch.setattr(mcp, "connect_mcp", fake_connect)

    result = await mcp.add_server([], "https://svc/mcp", bearer_token="t", **_kwargs(tmp_path))

    assert result.persisted is False
    assert not (tmp_path / "config.toml").exists()


async def test_add_server_rejects_a_url_already_connected(tmp_path):
    connections = [mcp.ServerConnection(url="https://svc/mcp", client=None, tools=[], auth_mode="none")]

    with pytest.raises(mcp.AlreadyConnected):
        await mcp.add_server(connections, "https://svc/mcp", **_kwargs(tmp_path))


async def test_add_server_wraps_a_connection_failure_but_not_a_bearer_challenge(monkeypatch, tmp_path):
    async def refusing(url, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mcp, "connect_mcp", refusing)
    with pytest.raises(mcp.ConnectFailed) as failure:
        await mcp.add_server([], "https://svc/mcp", **_kwargs(tmp_path))
    assert "connection refused" in str(failure.value)

    async def demanding(url, **kw):
        raise mcp.BearerTokenRequired(url)

    monkeypatch.setattr(mcp, "connect_mcp", demanding)
    with pytest.raises(mcp.BearerTokenRequired):
        await mcp.add_server([], "https://svc/mcp", **_kwargs(tmp_path))


async def test_add_server_refreshes_every_live_agents_delegate(monkeypatch, tmp_path):
    """A worker declaring the server resolves it on the next spawn, which is the only in-process reach."""

    async def fake_connect(url, **kw):
        return _FakeClient([]), "none"

    monkeypatch.setattr(mcp, "connect_mcp", fake_connect)
    refreshed = []

    await mcp.add_server(
        [],
        "https://svc/mcp",
        **_kwargs(tmp_path, for_each_agent=lambda fn: refreshed.append(fn)),
        refresh_workers=lambda agent: None,
    )

    assert len(refreshed) == 1


async def test_remove_server_closes_forgets_and_unpersists(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[[mcp.server]]\nurl = "https://svc/mcp"\nname = "svc"\n', encoding="utf-8")
    client = _FakeClient()
    connections = [mcp.ServerConnection(url="https://svc/mcp", client=client, tools=["a"], auth_mode="none")]

    result = await mcp.remove_server(
        connections, "https://svc/mcp", **_remove_kwargs(tmp_path, config_path=config_path)
    )

    assert result.freed_tool_names == ["a"]
    assert connections == [] and client.closed is True
    assert "https://svc/mcp" not in config_path.read_text(encoding="utf-8")


async def test_remove_server_reports_only_the_names_no_other_server_still_provides(tmp_path):
    going = mcp.ServerConnection(url="https://a/mcp", client=_FakeClient(), tools=["shared", "mine"], auth_mode="none")
    staying = mcp.ServerConnection(url="https://b/mcp", client=_FakeClient(), tools=["shared"], auth_mode="none")

    result = await mcp.remove_server([going, staying], "https://a/mcp", **_remove_kwargs(tmp_path))

    assert result.freed_tool_names == ["mine"]


async def test_remove_server_rejects_an_unconnected_url(tmp_path):
    with pytest.raises(mcp.NotConnected):
        await mcp.remove_server([], "https://svc/mcp", **_remove_kwargs(tmp_path))


# --- The boot reconnect ----------------------------------------------------------------------------


def _boot_config(*servers):
    from kokua.config import AssistantConfig

    return AssistantConfig(mcp_servers=list(servers))


async def test_boot_reconnect_logs_the_tool_names_each_server_contributed(monkeypatch, tmp_path, caplog):
    """The only record of a remote server's tool list, so a later "why didn't it call that?" is answerable.

    Neither config.toml nor ``--list-toolsets`` knows a remote server's tools, and ``add_mcp_server``
    reports them only on a runtime add, so without this line a boot leaves no trace of what a server
    actually provided.
    """

    async def fake_connect(url, **kw):
        return _FakeClient(["get_positions", "get_quote"]), "oauth"

    monkeypatch.setattr(mcp, "connect_mcp", fake_connect)
    config = _boot_config(MCPServerConfig(url="https://svc/mcp", name="svc"))

    with caplog.at_level("INFO", logger="kokua.mcp.servers"):
        await mcp.reconnect_mcp_servers(
            lambda fn: None, [], config, notify=_noop_notify, oauth=OAuthSettings(storage_dir=tmp_path / "oauth")
        )

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "get_positions" in logged and "get_quote" in logged


async def test_boot_reconnect_says_so_when_a_server_contributes_no_tools(monkeypatch, tmp_path, caplog):
    """A connected-but-empty server is the case worth naming: it looks identical to a healthy one."""

    async def fake_connect(url, **kw):
        return _FakeClient([]), "none"

    monkeypatch.setattr(mcp, "connect_mcp", fake_connect)
    config = _boot_config(MCPServerConfig(url="https://svc/mcp", name="svc"))

    with caplog.at_level("INFO", logger="kokua.mcp.servers"):
        await mcp.reconnect_mcp_servers(
            lambda fn: None, [], config, notify=_noop_notify, oauth=OAuthSettings(storage_dir=tmp_path / "oauth")
        )

    assert any("no tools" in record.getMessage() for record in caplog.records)


async def test_boot_reconnect_through_an_auth_challenge_posts_nothing_to_the_channel(monkeypatch, tmp_path):
    """Every OAuth server rediscovers its mode through a rejected request on every boot, silently.

    ``notify`` reaches the user's conversation, and only the OAuth provider's redirect handler may use
    it, when there is no usable cached token. A `notify` call from the challenge path itself would post
    a message on every single start, and would also destroy the signal that its absence from a log is
    what tells you token reuse is working.
    """
    posted = []

    async def recording_notify(message: str) -> None:
        posted.append(message)

    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise RuntimeError("Client error '401 Unauthorized'")
        return _FakeClient(["get_positions"])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    config = _boot_config(MCPServerConfig(url="https://svc/mcp", name="svc"))
    connections = []

    await mcp.reconnect_mcp_servers(
        lambda fn: None,
        connections,
        config,
        notify=recording_notify,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
    )

    assert posted == []
    assert [(c.url, c.auth_mode, c.tools) for c in connections] == [("https://svc/mcp", "oauth", ["get_positions"])]
