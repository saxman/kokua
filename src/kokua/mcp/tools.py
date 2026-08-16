"""The runtime ``add_mcp_server`` / ``remove_mcp_server`` agent tools.

`mcp/`'s entry in the ``<subsystem>/tools.py`` convention: agent tools live in their subsystem's
``tools.py``, and the connection machinery they drive stays in ``servers.py``. See
``docs/explanation/architecture.md`` for the full inventory of the entry agent's toolset.

``connect_mcp`` is called through the ``servers`` module rather than imported by name, deliberately:
``reconnect_mcp_servers`` calls it too, so a single patch point (``kokua.mcp.servers.connect_mcp``)
covers both the boot path and the runtime tool. Importing the name here would bind it at import time and
silently split that seam in two.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from aimu.tools import tool

from kokua.config import store as config_store
from kokua.mcp import servers
from kokua.mcp.auth import Notify
from kokua.mcp.servers import RECONNECTABLE, BearerTokenRequired, ForEachAgent
from kokua.toolsets.registry import Toolset

logger = logging.getLogger(__name__)


def make_mcp_tools(
    for_each_agent: ForEachAgent,
    connections: list,
    *,
    notify: Notify,
    oauth_storage_dir: Path,
    config_path: Path,
    refresh_workers: Optional[Callable] = None,
) -> list[Callable]:
    """Build the ``add_mcp_server`` / ``remove_mcp_server`` tools bound to the config file.

    Lets the assistant connect to (and disconnect from) a remote MCP service by URL mid-session, with
    the delegate rebuild fanned out to every live conversation's agent via ``for_each_agent``. A reconnectable
    server (unauthenticated or OAuth) is recorded in ``config.toml`` ``[[mcp.server]]`` so it reconnects
    on the next restart; bearer-token servers are session-only (their secret is not written to disk).
    ``connections`` is the live list shared with the boot path and teardown.

    Neither tool mounts anything on a live agent, and that bounds when a change takes effect. An agent's
    own tool list is built once, in ``wire_agent``; ``refresh_workers`` (applied to every live agent)
    replaces only that agent's ``spawn_subagent``. So a connect or disconnect reaches the *next*
    sub-agent spawned by an agent whose ``[agents.*].tools`` names the server, and reaches a live agent's
    own list only when that agent is rebuilt (a new conversation, an LRU eviction, or a restart). Note the
    consequence for a config where a live agent declares the server directly: it holds callables from the
    connection it was built with, so a disconnect leaves that agent with stale ones until it is rebuilt.
    """

    @tool
    async def add_mcp_server(url: str, bearer_token: Optional[str] = None) -> str:
        """Connect to a remote MCP server by URL so its tools can be used.

        An unauthenticated or OAuth connection is remembered and restored the next time the assistant
        starts; a bearer-token one lasts only this session, since its secret is never written to disk.
        Returns the tool names the server exposes and the rule for which agent can call them. You cannot
        call them yourself: delegate to a sub-agent whose configured tools name this server.

        Authentication is handled for you: just pass the URL. If the server is unprotected it
        connects directly. If it requires OAuth, you post an authorization link into the chat and
        open a browser window for the user to approve; once they do, the connection completes and
        the token is saved for future sessions. Do not claim you cannot authenticate or that this
        is impossible from here, that flow is built in. Pass bearer_token only when the user gives
        you a static token to use instead of the OAuth flow.

        Some servers require authentication but do not support the automatic OAuth flow. When that
        happens this returns a message asking for a bearer token: relay it, ask the user for a
        token, then call this tool again with that bearer_token.

        When a server is recorded, its name is derived from the URL, but that name reaches no agent
        until a human adds it to that agent's tools list in config.toml's [agents.*] section: that
        section is hand-edit only, so this tool cannot grant itself (or any agent) the new capability
        it just connected.
        """
        if any(conn.url == url for conn in connections):
            return f"Already connected to {url}. Use remove_mcp_server first if you need to reconnect it."
        try:
            client, auth_mode = await servers.connect_mcp(
                url, bearer_token=bearer_token, notify=notify, oauth_storage_dir=oauth_storage_dir
            )
            # Records the connection only; nothing is mounted on this agent, whose tool list is already
            # built. The rebuild below is what puts the server in the next sub-agent spawned.
            added = await servers.attach_server(connections, url, client, auth_mode)
        except BearerTokenRequired as exc:
            return str(exc)
        except Exception as exc:
            return f"Failed to connect to MCP server {url!r}: {exc}"
        if refresh_workers is not None:
            for_each_agent(refresh_workers)  # rebuild spawn_subagent so a worker declaring it resolves it
        # Persist reconnectable servers (no secret on disk); a bearer server stays session-only.
        if auth_mode in RECONNECTABLE:
            config_store.add_mcp_server(config_path, url)
            note = ""
        else:
            note = " (session only; add it to config.toml [mcp] to keep a bearer-token server across restarts)"
        names = ", ".join(added) if added else "(no new tools)"
        # A tool result, so it says what the model can do next rather than why. Both halves are load
        # bearing: a live agent's own tool list is fixed at wire_agent time and this rebuild only
        # replaces `spawn_subagent`, so the caller genuinely cannot call these itself, and only a
        # sub-agent spawned after this point resolves its tools against the new connection.
        reach = (
            "You cannot call these yourself. They reach a sub-agent you spawn from now on, if its "
            "[agents.*] tools list in config.toml names this server; only the user can add that name."
        )
        return f"Connected to {url}. Its tools: {names}. {reach}{note}"

    @tool
    async def remove_mcp_server(url: str) -> str:
        """Disconnect a remote MCP server added earlier and stop offering its tools.

        Closes the connection and forgets the server, so it is not reconnected on the next restart.
        A sub-agent spawned from then on will not have its tools. Pass the same URL used to add it.
        """
        entry = next((c for c in connections if c.url == url), None)
        if entry is None:
            return f"No MCP server is connected at {url!r}."

        # `entry.tools` lists every tool name this server exposes, but a same-named tool from another
        # still-connected server was never separately recorded; only report names this removal actually
        # frees up. Nothing is stripped from a live agent's own tool list, which is why the message below
        # promises only that a newly spawned sub-agent lacks them: under the shipped config no live agent
        # holds a server's callables anyway, and under a config where one declares the server directly,
        # stripping would mean editing a list this function does not own.
        still_owned = {name for conn in connections if conn is not entry for name in conn.tools}
        removed = set(entry.tools) - still_owned
        connections.remove(entry)
        if refresh_workers is not None:
            for_each_agent(refresh_workers)  # rebuild spawn_subagent so a worker declaring it stops seeing it
        try:
            await entry.client.aclose()
        except Exception:
            logger.debug("Error closing MCP client for %s", url, exc_info=True)
        config_store.remove_mcp_server(config_path, url)
        names = ", ".join(sorted(removed)) if removed else "(none)"
        # The mirror of the connect message, and wrong in the same way if it claimed the tools are gone
        # now: a live agent's tool list is not rewritten here either, so the change shows up in the next
        # sub-agent spawned rather than immediately.
        gone = "A sub-agent you spawn from now on will not have them."
        return f"Disconnected {url}. Tools no longer provided: {names}. {gone}"

    return [add_mcp_server, remove_mcp_server]


def _build(ctx) -> list:
    """Wire the MCP management tools to the live connection list and the agent fan-out.

    ``refresh_workers`` comes off the context rather than being imported: it lives in ``core.build``,
    which imports this module, so a direct import would be circular.
    """
    return make_mcp_tools(
        ctx.state.for_each_agent,
        ctx.state.connections,
        notify=ctx.state.notify,
        oauth_storage_dir=ctx.state.oauth_storage_dir,
        config_path=ctx.config.config_path,
        refresh_workers=ctx.state.refresh_workers,
    )


TOOLSET = Toolset(
    name="mcp-admin",
    description="Connect and disconnect remote MCP servers at runtime.",
    build=_build,
    cross_cutting=True,
)
