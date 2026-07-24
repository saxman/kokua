"""Unit tests for the auth-required heuristic that gates the runtime-add OAuth fallback."""

import pytest
from aimu import aio

from kokua import mcp
from kokua.config import MCPServerConfig
from kokua.mcp import _looks_like_auth_required, _looks_like_registration_unsupported


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
        "https://svc/mcp", bearer_token="tok", notify=_noop_notify, oauth_storage_dir=tmp_path
    )
    assert (client, mode) == ("client", "bearer")


async def test_connect_mcp_asks_for_bearer_when_registration_unsupported(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise Exception("401 Unauthorized")
        raise Exception("Registration failed: 404 404 page not found")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    with pytest.raises(mcp.BearerTokenRequired) as excinfo:
        await mcp.connect_mcp("https://git.example/mcp", notify=_noop_notify, oauth_storage_dir=tmp_path / "oauth")
    assert "git.example" in str(excinfo.value)
    assert "bearer token" in str(excinfo.value).lower()


async def test_connect_mcp_other_oauth_failure_reraises_unchanged(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise Exception("401 Unauthorized")
        raise RuntimeError("token exchange failed")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    with pytest.raises(RuntimeError, match="token exchange failed"):
        await mcp.connect_mcp("https://svc/mcp", notify=_noop_notify, oauth_storage_dir=tmp_path / "oauth")


async def test_add_mcp_server_tool_returns_bearer_instruction(monkeypatch, tmp_path):
    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:
            raise Exception("401 Unauthorized")
        raise Exception("Registration failed: 404")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    add_mcp_server, _ = mcp.make_mcp_tools(
        lambda fn: None,
        [],
        notify=_noop_notify,
        oauth_storage_dir=tmp_path / "oauth",
        registry_path=tmp_path / "registry.json",
    )
    msg = await add_mcp_server(url="https://git.example/mcp")
    assert "bearer token" in msg.lower()
    assert "git.example" in msg


def test_resolve_server_token_reads_env(monkeypatch):
    monkeypatch.setenv("MY_MCP_TOKEN", "secret")
    server = MCPServerConfig(url="https://svc/mcp", token_env="MY_MCP_TOKEN")
    assert mcp._resolve_server_token(server) == "secret"


def test_resolve_server_token_none_without_token_env():
    assert mcp._resolve_server_token(MCPServerConfig(url="https://svc/mcp")) is None


def test_resolve_server_token_warns_when_env_unset(monkeypatch, caplog):
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    server = MCPServerConfig(url="https://svc/mcp", token_env="MISSING_MCP_TOKEN")
    with caplog.at_level("WARNING"):
        assert mcp._resolve_server_token(server) is None
    assert any("MISSING_MCP_TOKEN" in rec.message for rec in caplog.records)


async def _noop_notify(message: str) -> None:
    pass
