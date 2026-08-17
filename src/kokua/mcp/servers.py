"""Remote-MCP connection management: connect, attach, the runtime add/remove, and the boot reconnect.

Split out of the assistant core (which only orchestrates); these functions touch the passed-in
connections list, not `Assistant` state. Lives alongside `auth.py` (the OAuth flow). A runtime-added
server is recorded straight into config.toml's ``[[mcp.server]]`` (via config_store), so config.toml is
the single source of servers to reconnect at the next startup.

Nothing here formats a sentence. ``add_server`` and ``remove_server`` return a result record or raise,
and the ``mcp-admin`` toolset renders what the model reads -- including the long explanation of what a
newly connected server can and cannot reach this session, which is presentation and belongs there.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from aimu import aio

from kokua.config import AssistantConfig, MCPServerConfig
from kokua.config import store as config_store
from kokua.mcp.auth import Notify, build_chat_oauth

logger = logging.getLogger(__name__)


# Auth modes a server can be reconnected in at boot without a stored secret: unauthenticated, or the
# persisted-token OAuth flow. A bearer-token server is session-only (its secret is never written to
# config.toml); persist that via a hand-authored [[mcp.server]] with a token_env instead.
RECONNECTABLE = ("none", "oauth")


class BearerTokenRequired(Exception):
    """A server needs authentication that the automatic OAuth flow can't obtain (no dynamic client
    registration), so the user must supply a bearer token. Its message is user-facing and actionable.
    """

    def __init__(self, url: str):
        super().__init__(
            f"The MCP server {url} requires authentication, but its OAuth flow can't complete because "
            "the server does not support automatic client registration. To connect, provide a bearer "
            "token (for example a personal access token from the service) and add the server again with "
            "that token."
        )


def _looks_like_auth_required(exc: BaseException) -> bool:
    """Heuristic: did this connection failure come from an auth challenge (so OAuth should run)?

    Matches the failure text against common auth signals (401/403, an authorization/
    authentication requirement, a WWW-Authenticate / OAuth hint). "authoriz"/"authentic" also
    catch servers that signal the requirement with a 400 + "missing Authorization header"
    instead of a standard 401. Deliberately narrow so a plain unreachable host (DNS, connection
    refused) does not trigger an OAuth attempt.
    """
    text = f"{exc} {getattr(exc, '__cause__', '') or ''}".lower()
    signals = ("401", "403", "authoriz", "authentic", "forbidden", "www-authenticate", "oauth")
    return any(s in text for s in signals)


def _looks_like_registration_unsupported(exc: BaseException) -> bool:
    """Did the OAuth flow fail because the server has no dynamic client registration endpoint?

    fastmcp auto-registers a client (RFC 7591) before the authorization redirect; a server without a
    ``/register`` endpoint fails that step (e.g. "Registration failed: 404"). That means OAuth can't
    proceed and the user must supply a bearer token instead.
    """
    text = f"{exc} {getattr(exc, '__cause__', '') or ''}".lower()
    return "registration" in text


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

    A ``bearer_token`` always takes precedence. Otherwise the connection tries unauthenticated first
    and falls back to the OAuth flow on an auth challenge, discovering the mode rather than being told
    it: nothing persists a server's auth mode across restarts, so every non-bearer server rediscovers
    it this way on every boot. That costs one rejected request per server per boot and buys having no
    stored mode that can go stale against a server that changed its mind.

    Passing ``auth_mode`` explicitly skips the discovery for a caller that already knows. No caller
    currently does; it exists for one that connects to a server it just probed.

    OAuth persists tokens under ``oauth_storage_dir``, so the usual path through the challenge branch
    below finds a cached token and connects without involving the user at all. ``notify`` posts an
    authorization link only when there is no usable token, which is why its absence from a log is the
    signal that token reuse is working.
    """
    if bearer_token:
        return await aio.MCPClient.connect(url=url, auth=bearer_token), "bearer"
    if auth_mode == "oauth":
        provider = build_chat_oauth(url, notify=notify, token_storage_dir=oauth_storage_dir)
        return await aio.MCPClient.connect(url=url, auth=provider), "oauth"
    if auth_mode == "none":
        return await aio.MCPClient.connect(url=url), "none"
    try:
        return await aio.MCPClient.connect(url=url), "none"
    except Exception as exc:
        if not _looks_like_auth_required(exc):
            raise
        # Deliberately not "starting OAuth flow": this is the routine path for every OAuth server on
        # every boot, and a cached token makes it silent. Wording it as the start of an interactive
        # flow reads like the user is about to be prompted, and sends anyone reading the log after a
        # tool came back empty off hunting a token-persistence bug that isn't there.
        logger.info("MCP server %s rejected an unauthenticated request; trying OAuth credentials.", url)
        provider = build_chat_oauth(url, notify=notify, token_storage_dir=oauth_storage_dir)
        try:
            return await aio.MCPClient.connect(url=url, auth=provider), "oauth"
        except Exception as oauth_exc:
            if _looks_like_registration_unsupported(oauth_exc):
                raise BearerTokenRequired(url) from oauth_exc
            raise


async def attach_server(connections: list, url: str, client: Any, auth_mode: str) -> list[str]:
    """Record a connected server and return the names of the tools it makes usable.

    This mounts nothing on any agent; it only records the connection. An agent reaches the server by
    naming it in its ``[agents.*].tools``, which is resolved when that agent (or a sub-agent's spec) is
    built, so recording the callables here is what lets a later build resolve against them without
    re-fetching. The full tool-name list is returned for the caller's message.
    """
    new_tools = await client.as_tools()
    added_names = [fn.__name__ for fn in new_tools]
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


class AlreadyConnected(Exception):
    """This URL is already in the live connection list. Carries the url."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


class NotConnected(Exception):
    """No live connection for this URL. Carries the url."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


class ConnectFailed(Exception):
    """A connection (or its tool fetch) failed for a reason that is not an auth challenge.

    Its message is the underlying failure's, so a caller can report the cause without unwrapping.
    """

    def __init__(self, url: str, cause: BaseException):
        super().__init__(str(cause))
        self.url = url
        self.cause = cause


@dataclass(frozen=True)
class ServerAdded:
    """The outcome of :func:`add_server`.

    ``persisted`` is False for a bearer-token server, which stays session-only because its secret is
    never written to disk. ``tool_names`` is everything the server exposes, not everything it newly
    makes callable, which is the asymmetry with :class:`ServerRemoved`.
    """

    url: str
    tool_names: list[str]
    auth_mode: str
    persisted: bool


@dataclass(frozen=True)
class ServerRemoved:
    """The outcome of :func:`remove_server`.

    ``freed_tool_names`` excludes any name a still-connected server also provides, since removing this
    one did not actually take those away.
    """

    url: str
    freed_tool_names: list[str]


async def add_server(
    connections: list,
    url: str,
    *,
    bearer_token: Optional[str] = None,
    notify: Notify,
    oauth_storage_dir: Path,
    config_path: Path,
    for_each_agent: ForEachAgent,
    refresh_workers: Optional[Callable] = None,
) -> ServerAdded:
    """Connect a remote MCP server at runtime and record it.

    Nothing is mounted on any live agent: an agent's own tool list was built once, and this only records
    the connection. ``refresh_workers``, fanned out over ``for_each_agent``, rebuilds each agent's
    ``spawn_subagent`` so a worker declaring this server resolves it. A reconnectable server (see
    :data:`RECONNECTABLE`) is written to config.toml so it returns on the next start.

    Raises :class:`AlreadyConnected`, :class:`BearerTokenRequired`, or :class:`ConnectFailed`.
    """
    if any(conn.url == url for conn in connections):
        raise AlreadyConnected(url)
    try:
        client, auth_mode = await connect_mcp(
            url, bearer_token=bearer_token, notify=notify, oauth_storage_dir=oauth_storage_dir
        )
        added = await attach_server(connections, url, client, auth_mode)
    except BearerTokenRequired:
        raise
    except Exception as exc:
        raise ConnectFailed(url, exc) from exc
    if refresh_workers is not None:
        for_each_agent(refresh_workers)  # rebuild spawn_subagent so a worker declaring it resolves it
    persisted = auth_mode in RECONNECTABLE
    if persisted:
        config_store.add_mcp_server(config_path, url)
    return ServerAdded(url=url, tool_names=added, auth_mode=auth_mode, persisted=persisted)


async def remove_server(
    connections: list,
    url: str,
    *,
    config_path: Path,
    for_each_agent: ForEachAgent,
    refresh_workers: Optional[Callable] = None,
) -> ServerRemoved:
    """Disconnect a server added earlier, forget it, and stop reconnecting it at startup.

    Nothing is stripped from a live agent's own tool list, which this does not own; the change reaches
    the next sub-agent spawned, via the same ``refresh_workers`` fan-out ``add_server`` uses.

    Raises :class:`NotConnected`.
    """
    entry = next((conn for conn in connections if conn.url == url), None)
    if entry is None:
        raise NotConnected(url)

    # `entry.tools` lists every tool name this server exposes, but a same-named tool from another
    # still-connected server was never separately recorded; only report names this removal actually
    # frees up.
    still_owned = {name for conn in connections if conn is not entry for name in conn.tools}
    freed = sorted(set(entry.tools) - still_owned)
    connections.remove(entry)
    if refresh_workers is not None:
        for_each_agent(refresh_workers)  # rebuild spawn_subagent so a worker declaring it stops seeing it
    try:
        await entry.client.aclose()
    except Exception:
        logger.debug("Error closing MCP client for %s", url, exc_info=True)
    config_store.remove_mcp_server(config_path, url)
    return ServerRemoved(url=url, freed_tool_names=freed)


def _resolve_server_token(server: MCPServerConfig) -> Optional[str]:
    """Read a startup server's bearer token from its ``token_env`` environment variable.

    Returns ``None`` when no ``token_env`` is configured. If one is configured but the variable is
    unset or empty, logs a warning and returns ``None`` so the assistant still starts (the connection
    then proceeds tokenless, surfacing the auth requirement) rather than crashing on a missing secret.
    """
    if not server.token_env:
        return None
    token = os.environ.get(server.token_env)
    if not token:
        logger.warning(
            "MCP server %s: environment variable %s is unset; connecting without a token.",
            server.url,
            server.token_env,
        )
        return None
    return token


async def reconnect_mcp_servers(
    for_each_agent: ForEachAgent,
    connections: list,
    config: AssistantConfig,
    *,
    notify: Notify,
    oauth_storage_dir: Path,
) -> None:
    """Reconnect MCP servers at boot so their tools are available without re-adding them.

    All servers now live in config.toml ``[[mcp.server]]`` (both hand-authored bearer-token servers and
    runtime-added ones the tool recorded there), so this is a single pass over ``config.mcp_servers``. A
    connect failure logs and continues so one unreachable server can't stop the assistant from starting.
    Each connection is recorded in ``connections``, so an agent naming it resolves against it when built,
    including conversations built later. Boot deliberately connects before the first agent is built, which
    is what lets a config-declared server reach an agent's own tool list at all rather than only its next
    spawned sub-agent, and which is also why ``for_each_agent`` fans out to nothing here: no agent is live
    yet. It is taken anyway so this shares one signature with the runtime add, where the fan-out matters.

    Each success logs the tools the server contributed, because nothing else in the process will: see the
    comment on that line.
    """
    for server in config.mcp_servers:
        try:
            client, mode = await connect_mcp(
                server.url,
                bearer_token=_resolve_server_token(server),
                notify=notify,
                oauth_storage_dir=oauth_storage_dir,
            )
            added = await attach_server(connections, server.url, client, mode)
            # The names, not just the count: a remote server's tool list is the one part of an agent's
            # capability this repository cannot show you: it is not in config.toml, not in
            # `--list-toolsets` (which runs before any connection), and no tool reports it, since
            # `add_mcp_server` announces its own tools only on a runtime add. Without this line, an
            # agent that answered from its own knowledge instead of calling a server's tool leaves no
            # way to tell whether the tool it needed was missing or merely unused.
            logger.info(
                "MCP server %s connected (%s): %s",
                server.url,
                mode,
                ", ".join(added) if added else "no tools",
            )
        except Exception:
            logger.warning("Could not connect MCP server %s; continuing without it.", server.url, exc_info=True)
