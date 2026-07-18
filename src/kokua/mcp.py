"""Remote-MCP connection management: connect/attach helpers and the runtime add/remove tools.

Split out of the assistant core (which only orchestrates); these functions touch the passed-in
agent and connections list, not `Assistant` state. Lives alongside `mcp_auth.py` (the OAuth flow)
and `mcp_registry.py` (the reconnect-across-restarts record).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from aimu import aio
from aimu.tools import tool

from . import mcp_registry
from .config import AssistantConfig
from .mcp_auth import Notify, build_chat_oauth

logger = logging.getLogger(__name__)


def _looks_like_auth_required(exc: BaseException) -> bool:
    """Heuristic: did this connection failure come from an auth challenge (so OAuth should run)?

    Matches the failure text against common auth signals (401/403, "unauthorized", a
    WWW-Authenticate / OAuth hint). Deliberately narrow so a plain unreachable host (DNS,
    connection refused) does not trigger an OAuth attempt.
    """
    text = f"{exc} {getattr(exc, '__cause__', '') or ''}".lower()
    return any(s in text for s in ("401", "403", "unauthor", "forbidden", "www-authenticate", "oauth"))


# Applies a per-agent mutation to every live agent. Injected so add/remove fan the change out across
# all conversations instead of touching a single captured agent.
ForEachAgent = Callable[[Callable[[Any], None]], None]


@dataclass
class ServerConnection:
    """A live remote-MCP connection and the tools it contributed (for teardown and removal)."""

    url: str
    client: Any  # aio.MCPClient
    tools: list[str]  # __name__ of each tool this server added to agent.tools
    auth_mode: str  # "none" | "oauth" | "bearer"
    callables: list = field(default_factory=list)  # the tool callables, so a new agent can reattach without re-fetch


async def connect_mcp(
    url: str,
    *,
    bearer_token: Optional[str] = None,
    auth_mode: Optional[str] = None,
    notify: Notify,
    oauth_storage_dir: Path,
) -> tuple[Any, str]:
    """Connect to a remote MCP server, returning ``(client, auth_mode_used)``.

    With ``auth_mode`` known (a boot reconnect) the connection uses that mode directly. With it
    ``None`` (a runtime add) the connection tries unauthenticated first and falls back to the OAuth
    flow on an auth challenge. A ``bearer_token`` always takes precedence. OAuth posts an
    authorization link via ``notify`` and persists tokens under ``oauth_storage_dir`` (so a cached
    token reconnects silently).
    """
    if bearer_token:
        return await aio.MCPClient.connect(url=url, auth=bearer_token), "bearer"
    if auth_mode == "oauth":
        provider = build_chat_oauth(url, notify=notify, token_storage_dir=oauth_storage_dir)
        return await aio.MCPClient.connect(url=url, auth=provider), "oauth"
    if auth_mode == "none":
        return await aio.MCPClient.connect(url=url), "none"
    # Unknown (runtime add): try unauthenticated, fall back to OAuth on an auth challenge.
    try:
        return await aio.MCPClient.connect(url=url), "none"
    except Exception as exc:
        if not _looks_like_auth_required(exc):
            raise
        logger.info("MCP server %s requires authorization; starting OAuth flow.", url)
        provider = build_chat_oauth(url, notify=notify, token_storage_dir=oauth_storage_dir)
        return await aio.MCPClient.connect(url=url, auth=provider), "oauth"


async def attach_server(
    for_each_agent: ForEachAgent, connections: list, url: str, client: Any, auth_mode: str
) -> list[str]:
    """Add a connected server's tools to every live agent (deduped per agent) and record the connection.

    Returns the names of the tools newly added on at least one agent. Tools land on each ``agent.tools``;
    the tool-loop engine re-reads the agent's effective tools each round, so a server added mid-turn is
    dispatchable in the same turn without touching the model client. The connection stores the tool
    callables so a lazily-built agent can reattach them at build time without re-fetching.
    """
    new_tools = await client.as_tools()
    added_names: list[str] = []

    def extend(agent):
        existing = {getattr(fn, "__name__", None) for fn in agent.tools}
        to_add = [fn for fn in new_tools if fn.__name__ not in existing]
        agent.tools.extend(to_add)
        for fn in to_add:
            if fn.__name__ not in added_names:
                added_names.append(fn.__name__)

    for_each_agent(extend)
    connections.append(
        ServerConnection(
            url=url,
            client=client,
            tools=[fn.__name__ for fn in new_tools],
            auth_mode=auth_mode,
            callables=list(new_tools),
        )
    )
    return added_names


async def reconnect_mcp_servers(
    for_each_agent: ForEachAgent,
    connections: list,
    config: AssistantConfig,
    *,
    notify: Notify,
    oauth_storage_dir: Path,
) -> None:
    """Reconnect MCP servers at boot so their tools are available without re-adding them.

    First the ones declared in config (--mcp / [mcp] servers), then the ones added at runtime and
    recorded in the registry (deduped by URL). A connect failure logs and continues so one unreachable
    server can't stop the assistant from starting. Each connection is recorded in ``connections`` (so
    ``build_agent`` attaches it to conversations built later) and fanned out to whatever agents are live
    at boot (initially just the active one).
    """
    for url in config.mcp_servers:
        try:
            client, mode = await connect_mcp(
                url, bearer_token=config.mcp_bearer, notify=notify, oauth_storage_dir=oauth_storage_dir
            )
            await attach_server(for_each_agent, connections, url, client, mode)
        except Exception:
            logger.warning("Could not connect MCP server %s; continuing without it.", url, exc_info=True)

    connected_urls = {conn.url for conn in connections}
    for record in mcp_registry.load(config.mcp_servers_path):
        url = record["url"]
        if url in connected_urls:
            continue
        try:
            client, mode = await connect_mcp(
                url, auth_mode=record.get("auth_mode"), notify=notify, oauth_storage_dir=oauth_storage_dir
            )
            await attach_server(for_each_agent, connections, url, client, mode)
        except Exception:
            logger.warning("Could not reconnect MCP server %s; continuing without it.", url, exc_info=True)


def make_mcp_tools(
    for_each_agent: ForEachAgent,
    connections: list,
    *,
    notify: Notify,
    oauth_storage_dir: Path,
    registry_path: Path,
) -> list[Callable]:
    """Build the ``add_mcp_server`` / ``remove_mcp_server`` tools bound to one connection registry.

    Lets the assistant connect to (and disconnect from) a remote MCP service by URL mid-session, with
    the change fanned out to every live conversation's agent via ``for_each_agent``. A reconnectable
    server (unauthenticated or OAuth) is recorded in ``registry_path`` so it reconnects on the next
    restart; bearer-token servers are session-only (their secret is not written to disk).
    ``connections`` is the live list shared with the boot path and teardown.
    """

    @tool
    async def add_mcp_server(url: str, bearer_token: Optional[str] = None) -> str:
        """Connect to a remote MCP server by URL and add its tools to this assistant.

        The server's tools become callable immediately, even in this same turn, and the connection
        is remembered so it is restored automatically the next time the assistant starts. Returns
        the names of the newly available tools.

        Authentication is handled for you: just pass the URL. If the server is unprotected it
        connects directly. If it requires OAuth, you post an authorization link into the chat and
        open a browser window for the user to approve; once they do, the connection completes and
        the token is saved for future sessions. Do not claim you cannot authenticate or that this
        is impossible from here, that flow is built in. Pass bearer_token only when the user gives
        you a static token to use instead of the OAuth flow.
        """
        if any(conn.url == url for conn in connections):
            return f"Already connected to {url}; its tools are available. Use remove_mcp_server to disconnect first."
        try:
            client, auth_mode = await connect_mcp(
                url, bearer_token=bearer_token, notify=notify, oauth_storage_dir=oauth_storage_dir
            )
            added = await attach_server(for_each_agent, connections, url, client, auth_mode)
        except Exception as exc:
            return f"Failed to connect to MCP server {url!r}: {exc}"
        # Persist reconnectable servers (no secret on disk); a bearer server stays session-only.
        if auth_mode in mcp_registry.RECONNECTABLE:
            mcp_registry.add(registry_path, url, auth_mode)
            note = ""
        else:
            note = " (session only; add it to config.toml [mcp] to keep a bearer-token server across restarts)"
        names = ", ".join(added) if added else "(no new tools)"
        return f"Connected to {url}. Tools now available: {names}.{note}"

    @tool
    async def remove_mcp_server(url: str) -> str:
        """Disconnect a remote MCP server added earlier and remove its tools.

        Drops the server's tools, closes the connection, and forgets it so it is not reconnected on
        the next restart. Pass the same URL that was used to add it.
        """
        entry = next((c for c in connections if c.url == url), None)
        if entry is None:
            return f"No MCP server is connected at {url!r}."

        # `entry.tools` lists every tool name this server exposes, but a same-named tool from
        # another still-connected server (or a built-in) was deduped at attach time and never
        # re-added; only strip names this removal actually frees up.
        still_owned = {name for conn in connections if conn is not entry for name in conn.tools}
        removed = set(entry.tools) - still_owned

        # Drop from every agent's tools; the engine re-reads the effective tools each round, so the
        # tools stop being advertised and dispatchable from the next round on (this turn included).
        def strip(agent):
            agent.tools[:] = [fn for fn in agent.tools if getattr(fn, "__name__", None) not in removed]

        for_each_agent(strip)
        connections.remove(entry)
        try:
            await entry.client.aclose()
        except Exception:
            logger.debug("Error closing MCP client for %s", url, exc_info=True)
        mcp_registry.remove(registry_path, url)
        names = ", ".join(sorted(removed)) if removed else "(none)"
        return f"Disconnected {url}. Removed tools: {names}."

    return [add_mcp_server, remove_mcp_server]
