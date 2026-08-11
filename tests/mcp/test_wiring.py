"""MCP servers wired into a live assistant: startup, runtime add/remove, OAuth, persistence.

An MCP server's tools reach WORKERS, never the supervisor, and only the workers whose roles name the
server. So these tests configure a role that names the server under test and then assert on that role's
resolved toolset (``_worker_tools``) rather than on ``assistant._agent.tools``.
"""

from __future__ import annotations

import pytest


from kokua.config import MCPServerConfig
from kokua.core.assistant import Assistant
from tests.channels import FakeChannel, _config
from tests.fakes import _FakeMCP, _await_value, _fake_mcp_tool
from tests.helpers import MockAsyncModelClient


def _using(url: str) -> dict:
    """Config override adding a role that draws on ``url``, so that server's tools reach a worker."""
    return {"subagent_roles": {"remote": {"description": "Uses the server.", "mcp_servers": [url]}}}


@pytest.fixture
def delegates(monkeypatch):
    """Record the ``agent_types`` every ``spawn_subagent`` delegate is built with, newest last.

    A runtime MCP add or remove is applied by REBUILDING each live agent's delegate, since the
    supervisor never holds MCP callables itself. Capturing what each rebuild was handed is how to see
    that the change actually reached that agent's workers; the supervisor's own tool list is the wrong
    place to look and would make these assertions vacuous.
    """
    import kokua.core.build as build_mod

    built: list[set[str]] = []
    real = build_mod.make_async_subagent_tool

    def capturing(model, **kwargs):
        types = kwargs.get("agent_types") or {}
        built.append({fn.__name__ for spec in types.values() for fn in spec["tools"]})
        return real(model, **kwargs)

    monkeypatch.setattr(build_mod, "make_async_subagent_tool", capturing)
    return built


def _worker_tools(assistant) -> set[str]:
    """The tools a worker would receive from the assistant's live MCP connections.

    Resolved against a role naming every connected server, rather than making each test declare one:
    these tests are about connection plumbing (connect, retry, persist, reconnect, remove), and which
    roles name which server is covered in ``tests/core/test_build.py``.
    """
    from dataclasses import replace

    from kokua.core.build import _build_subagent_agent_types

    role = {"description": "Uses every connected server.", "mcp_servers": [c.url for c in assistant._mcp_servers]}
    config = replace(assistant._config, subagent_roles={"remote": role})
    types = _build_subagent_agent_types(config, assistant._mcp_servers, {})
    return {fn.__name__ for spec in types.values() for fn in spec["tools"]}


async def test_startup_mcp_servers_wire_tools(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        assert auth == "tok"
        return _FakeMCP([_fake_mcp_tool("remote_search"), _fake_mcp_tool("remote_fetch")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    monkeypatch.setenv("SVC_TOKEN", "tok")
    assistant = await Assistant.create(
        _config(tmp_path, mcp_servers=[MCPServerConfig(url="https://svc/mcp", token_env="SVC_TOKEN")]),
        FakeChannel(),
        client=MockAsyncModelClient([]),
    )
    names = _worker_tools(assistant)
    assert {"remote_search", "remote_fetch"} <= names
    assert len(assistant._mcp_servers) == 1


async def test_startup_mcp_connect_failure_does_not_crash(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(
        _config(tmp_path, mcp_servers=[MCPServerConfig(url="https://down/mcp")]),
        FakeChannel(),
        client=MockAsyncModelClient([]),
    )
    assert assistant._mcp_servers == []


async def test_add_mcp_server_tool_adds_tools_at_runtime(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")
    assert add_mcp.__tool_is_async__ is True

    msg = await add_mcp(url="https://svc/mcp")
    assert "remote_search" in msg
    assert "remote_search" in _worker_tools(assistant)
    assert len(assistant._mcp_servers) == 1

    msg2 = await add_mcp(url="https://svc/mcp")
    assert "Already connected" in msg2
    assert len(assistant._mcp_servers) == 1
    assert sorted(_worker_tools(assistant)).count("remote_search") == 1


async def test_add_mcp_server_tool_reports_connect_failure(tmp_path, monkeypatch):
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")

    msg = await add_mcp(url="https://down/mcp")
    assert "Failed to connect" in msg and "boom" in msg
    assert assistant._mcp_servers == []


async def test_add_mcp_server_auto_oauth_on_auth_challenge(tmp_path, monkeypatch):
    """A tokenless connect that hits a 401 transparently retries with a ChatOAuth provider."""
    from aimu import aio

    from kokua.mcp.auth import ChatOAuth

    attempts = []

    async def fake_connect(*, url=None, auth=None, **kw):
        attempts.append(auth)
        if auth is None:  # first, unauthenticated attempt -> server challenges
            raise RuntimeError("failed to connect: Client error '401 Unauthorized'")
        return _FakeMCP([_fake_mcp_tool("remote_trade")])  # the OAuth-provider attempt succeeds

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")

    msg = await add_mcp(url="https://svc/mcp")  # no bearer token -> auto OAuth on the 401
    assert attempts[0] is None  # first attempt unauthenticated
    assert isinstance(attempts[1], ChatOAuth)  # retried with a chat-link OAuth provider
    assert "remote_trade" in msg
    assert "remote_trade" in _worker_tools(assistant)
    # Tokens persist under the app data dir so a later reconnect is silent.
    assert (tmp_path / "mcp-oauth").exists()


async def test_add_mcp_server_no_oauth_on_non_auth_failure(tmp_path, monkeypatch):
    """A non-auth failure (unreachable host) is reported without an OAuth attempt (no browser)."""
    from aimu import aio

    attempts = []

    async def fake_connect(*, url=None, auth=None, **kw):
        attempts.append(auth)
        raise RuntimeError("Connection refused")

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in assistant._agent.tools if t.__name__ == "add_mcp_server")

    msg = await add_mcp(url="https://down/mcp")
    assert attempts == [None]  # did not escalate to OAuth
    assert "Failed to connect" in msg


def _persisted_servers(cfg):
    """The [[mcp.server]] entries settings.load reads back from the assistant's config.toml."""
    from kokua.config import file as settings

    if not cfg.config_path.exists():
        return []
    return settings.load(str(cfg.config_path)).get("mcp_servers", [])


def _restart_config(tmp_path, cfg):
    """A fresh config for a simulated restart, loading persisted MCP servers as resolve_config would."""
    return _config(tmp_path, mcp_servers=_persisted_servers(cfg))


async def test_runtime_added_server_persists_and_reconnects(tmp_path, monkeypatch):
    """A server added at runtime is recorded in config.toml and reconnected on the next start."""
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    await add_mcp(url="https://svc/mcp")
    a1._store.close()
    # Recorded in config.toml [[mcp.server]] with just the URL (no secret on disk).
    assert [(s.url, s.token_env) for s in _persisted_servers(cfg)] == [("https://svc/mcp", None)]

    # Simulate a restart: a fresh Assistant reconnects from config.toml without re-adding.
    a2 = await Assistant.create(_restart_config(tmp_path, cfg), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_search" in _worker_tools(a2)
    assert [conn.url for conn in a2._mcp_servers] == ["https://svc/mcp"]


async def test_oauth_server_persists_and_reconnects_with_provider(tmp_path, monkeypatch):
    """An OAuth server is recorded (URL only) and reconnects via the provider on the auth challenge."""
    from aimu import aio

    from kokua.mcp.auth import ChatOAuth

    async def fake_connect(*, url=None, auth=None, **kw):
        if auth is None:  # unauthenticated attempt -> challenge
            raise RuntimeError("Client error '401 Unauthorized'")
        return _FakeMCP([_fake_mcp_tool("remote_trade")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    await add_mcp(url="https://svc/mcp")
    a1._store.close()
    # No auth_mode is stored, only the URL; reconnect rediscovers OAuth via the challenge path.
    assert [(s.url, s.token_env) for s in _persisted_servers(cfg)] == [("https://svc/mcp", None)]

    seen = []

    async def fake_connect2(*, url=None, auth=None, **kw):
        seen.append(auth)
        if auth is None:  # a plain attempt first, then the OAuth provider on the challenge
            raise RuntimeError("Client error '401 Unauthorized'")
        return _FakeMCP([_fake_mcp_tool("remote_trade")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect2)
    a2 = await Assistant.create(_restart_config(tmp_path, cfg), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_trade" in _worker_tools(a2)
    assert any(isinstance(auth, ChatOAuth) for auth in seen)  # reconnected via the OAuth provider


async def test_bearer_server_not_persisted(tmp_path, monkeypatch):
    """A bearer-token server is session-only: its secret is never written, so it is not reconnected."""
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_trade")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    msg = await add_mcp(url="https://svc/mcp", bearer_token="secret")
    a1._store.close()
    assert "session only" in msg
    assert _persisted_servers(cfg) == []

    a2 = await Assistant.create(_restart_config(tmp_path, cfg), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_trade" not in {fn.__name__ for fn in a2._agent.tools}


async def test_remove_mcp_server_drops_tools_and_forgets(tmp_path, monkeypatch):
    """remove_mcp_server removes the live tools and the config.toml record, so no reconnect on restart."""
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    cfg = _config(tmp_path)

    a1 = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    add_mcp = next(t for t in a1._agent.tools if t.__name__ == "add_mcp_server")
    remove_mcp = next(t for t in a1._agent.tools if t.__name__ == "remove_mcp_server")
    await add_mcp(url="https://svc/mcp")

    assert await remove_mcp(url="https://nope/mcp") == "No MCP server is connected at 'https://nope/mcp'."

    msg = await remove_mcp(url="https://svc/mcp")
    assert "Disconnected" in msg and "remote_search" in msg
    assert "remote_search" not in _worker_tools(a1)
    assert a1._mcp_servers == []
    assert _persisted_servers(cfg) == []
    a1._store.close()

    # Restart: the removed server is not reconnected.
    a2 = await Assistant.create(_restart_config(tmp_path, cfg), FakeChannel(), client=MockAsyncModelClient([]))
    assert "remote_search" not in _worker_tools(a2)


async def test_runtime_added_server_reaches_workers_in_the_same_turn(tmp_path, monkeypatch, delegates):
    """A server added mid-turn is usable by a worker spawned later in that same turn.

    The supervisor never carries MCP callables, so what has to happen immediately is a delegate
    REBUILD: add_mcp_server re-resolves every role against the new connection and swaps spawn_subagent,
    and remove_mcp_server does the reverse. Without that, a runtime-added server would only reach
    workers after the conversation's agent was rebuilt.
    """
    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("get_portfolio")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    assistant = await Assistant.create(
        _config(tmp_path, **_using("https://svc/mcp")), FakeChannel(), client=MockAsyncModelClient([])
    )
    agent = assistant._agent
    assert "get_portfolio" not in delegates[-1]  # not connected yet

    add_mcp = next(t for t in agent.tools if t.__name__ == "add_mcp_server")
    await add_mcp(url="https://svc/mcp")
    assert "get_portfolio" in delegates[-1]  # the delegate was rebuilt with it, this turn

    remove_mcp = next(t for t in agent.tools if t.__name__ == "remove_mcp_server")
    await remove_mcp(url="https://svc/mcp")
    assert "get_portfolio" not in delegates[-1]


async def test_add_mcp_server_fans_out_to_all_live_agents(tmp_path, monkeypatch, delegates):
    """Adding a server at runtime reaches every live agent's workers, not just the active one."""
    cfg = _config(tmp_path, **_using("https://example/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    await assistant.new_conversation()
    await assistant.select_conversation(first)
    assert len(assistant._registry.live_agents()) == 2

    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp",
        lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("remote_ping")]), "none")),
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    delegates.clear()
    await add_tool("https://example/mcp")

    # One rebuild per live agent, each carrying the new server's tools.
    assert len(delegates) == 2
    assert all("remote_ping" in rebuilt for rebuilt in delegates)


async def test_remove_mcp_server_fans_out_to_all_live_agents(tmp_path, monkeypatch, delegates):
    """Removing a server drops its tools from every live agent's workers."""
    cfg = _config(tmp_path, **_using("https://example/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    first = assistant._active_id
    await assistant.new_conversation()
    await assistant.select_conversation(first)

    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp",
        lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("remote_ping")]), "none")),
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_tool("https://example/mcp")
    assert all("remote_ping" in rebuilt for rebuilt in delegates[-2:])  # the add landed on both

    remove_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    delegates.clear()
    await remove_tool("https://example/mcp")

    assert len(delegates) == 2
    assert all("remote_ping" not in rebuilt for rebuilt in delegates)


async def test_remove_mcp_server_keeps_tool_still_owned_by_another_server(tmp_path, monkeypatch):
    """Removing a server must not strip a tool name still owned by another live connection.

    Two servers both expose a tool named "shared_tool". Attach dedups by __name__, so the second
    server's as_tools() call adds nothing new to agent.tools, but ServerConnection.tools still
    records "shared_tool" for both connections (it stores all of a server's tool names, not just
    the ones it uniquely added). Removing the second server must not strip the tool, because the
    first server still owns and exposes it.
    """
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp",
        lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("shared_tool")]), "none")),
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_tool(url="https://server-a/mcp")
    await add_tool(url="https://server-b/mcp")

    remove_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "remove_mcp_server")
    msg = await remove_tool(url="https://server-b/mcp")

    assert "shared_tool" in _worker_tools(assistant)
    assert "shared_tool" not in msg


async def test_newly_built_agent_gets_already_connected_server(tmp_path, monkeypatch, delegates):
    """A conversation whose agent is built after a server was added still reaches that server.

    The new agent resolves its roles against the shared `connections` list, so it needs no fan-out.
    """
    cfg = _config(tmp_path, **_using("https://example/mcp"))
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))

    monkeypatch.setattr(
        "kokua.mcp.servers.connect_mcp",
        lambda *a, **k: _await_value((_FakeMCP([_fake_mcp_tool("remote_ping")]), "none")),
    )
    add_tool = next(t for t in assistant._agent.tools if getattr(t, "__name__", "") == "add_mcp_server")
    await add_tool("https://example/mcp")

    delegates.clear()
    new_id = await assistant.new_conversation()
    assistant._registry.get(new_id)
    assert len(delegates) == 1  # built once, with no fan-out rebuild needed
    assert "remote_ping" in delegates[0]  # and already carrying the connected server


async def test_mcp_server_no_role_names_is_warned_about(tmp_path, monkeypatch, caplog):
    """A lean supervisor mounts no MCP callables, so a server only reaches the workers whose roles name
    it. An unreferenced server therefore connects, holds a token, and is reachable by nobody -- silently,
    until now."""
    import logging

    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    config = _config(
        tmp_path,
        mcp_servers=[MCPServerConfig(url="https://orphan/mcp", name="orphan")],
        subagent_roles={"coder": {"description": "Codes.", "groups": ["fs"]}},
    )
    with caplog.at_level(logging.WARNING, logger="kokua.core.assistant"):
        await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    assert any("orphan" in record.getMessage() for record in caplog.records)


async def test_mcp_server_a_role_names_is_not_warned_about(tmp_path, monkeypatch, caplog):
    import logging

    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    config = _config(
        tmp_path,
        mcp_servers=[MCPServerConfig(url="https://named/mcp", name="named")],
        subagent_roles={"r": {"description": "Uses it.", "mcp_servers": ["named"]}},
    )
    with caplog.at_level(logging.WARNING, logger="kokua.core.assistant"):
        await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    assert not any("named" in record.getMessage() for record in caplog.records)


async def test_a_role_may_name_an_mcp_server_by_raw_url(tmp_path, monkeypatch, caplog):
    """`name` is optional, so the warning has to accept a raw-URL reference as a match too."""
    import logging

    from aimu import aio

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("remote_search")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    config = _config(
        tmp_path,
        mcp_servers=[MCPServerConfig(url="https://byurl/mcp")],
        subagent_roles={"r": {"description": "Uses it.", "mcp_servers": ["https://byurl/mcp"]}},
    )
    with caplog.at_level(logging.WARNING, logger="kokua.core.assistant"):
        await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    assert not any("byurl" in record.getMessage() for record in caplog.records)
