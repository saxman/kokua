"""The ``mcp-admin`` toolset: ``add_mcp_server`` / ``remove_mcp_server`` over ``kokua.mcp.servers``.

The connection machinery is in ``mcp/servers.py``; everything here is what the model reads. That split
matters most for the docstrings below, which are long because they are load-bearing: both tools have to
tell the model exactly how far a runtime change reaches, or it promises the user a capability it will
not have this session.

Two separate things bound when a change takes effect. Distinguish the cases, because only one of them
is reachable in-process:

* **A server already declared in ``[[mcp.server]]`` at startup** (possibly unreachable then, or removed
  earlier this session) has a name in the toolset registry, so an agent's ``tools`` may already
  reference it. Connecting or disconnecting it reaches the *next* sub-agent spawned by such an agent,
  because ``refresh_workers`` rebuilds that agent's ``spawn_subagent`` and the specs re-resolve against
  the live connections. It does not reach a live agent's *own* tool list, which ``wire_agent`` built
  once; that waits for the agent to be rebuilt (a new conversation, an LRU eviction, a restart).
* **A genuinely new server reaches nothing at all until a restart.** ``build_registry`` runs once, in
  ``Assistant.create``, over ``config.mcp_servers`` as loaded at startup, and nothing mutates the
  registry afterwards, so a new server's name is not a key in it. No ``[agents.*]`` edit can reference
  that name either, since ``config.toml`` is not reread mid-process. ``add_mcp_server`` therefore
  persists the server for the *next* start and connects it for nothing else. This is the dominant flow,
  and both tool results say so.

One consequence worth knowing for a config where a live agent declares a server directly: it holds
callables from the connection it was built with, so a disconnect leaves that agent with stale ones until
it is rebuilt.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from aimu.tools import tool

from kokua.mcp import servers
from kokua.mcp.servers import AlreadyConnected, BearerTokenRequired, ConnectFailed, NotConnected
from kokua.toolsets.registry import Toolset

logger = logging.getLogger(__name__)

# The qualifier that is easy to lose and load-bearing: the toolset registry is built once in
# Assistant.create from the config as loaded then, so a server that was not already a [[mcp.server]]
# entry at startup has no name in it, and no [agents.*] edit can reference one (config.toml is not
# reread mid-process either). Only a restart makes a new server referable.
_REACH = (
    "You cannot call these yourself. A sub-agent you spawn can, but only if config.toml already "
    "declared this server at startup and names it in that agent's tools; otherwise the user must "
    "add both and restart Kokua, and nothing you do in this session will make them usable."
)


def make_mcp_tools(
    for_each_agent,
    connections: list,
    *,
    notify,
    oauth_storage_dir,
    config_path,
    refresh_workers: Optional[Callable] = None,
) -> list[Callable]:
    """Build the runtime add/remove tools over the live connection list and the agent fan-out."""

    @tool
    async def add_mcp_server(url: str, bearer_token: Optional[str] = None) -> str:
        """Connect to a remote MCP server by URL so its tools can be used.

        An unauthenticated or OAuth connection is remembered and restored the next time the assistant
        starts; a bearer-token one lasts only this session, since its secret is never written to disk.
        Returns the tool names the server exposes and the rule for which agent can call them. You cannot
        call them yourself. A sub-agent can, but only if this server was already declared in config.toml
        at startup and named in that agent's tools; a server new to this session is usable only after the
        user edits config.toml and restarts, so do not promise to use it in this conversation.

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
        until a human adds it to that agent's tools list in config.toml's [agents.*] section AND
        restarts Kokua: that section is hand-edit only and is read only at startup, so this tool
        cannot grant itself (or any agent) the capability it just connected.
        """
        try:
            result = await servers.add_server(
                connections,
                url,
                bearer_token=bearer_token,
                notify=notify,
                oauth_storage_dir=oauth_storage_dir,
                config_path=config_path,
                for_each_agent=for_each_agent,
                refresh_workers=refresh_workers,
            )
        except AlreadyConnected:
            return f"Already connected to {url}. Use remove_mcp_server first if you need to reconnect it."
        except BearerTokenRequired as exc:
            return str(exc)
        except ConnectFailed as exc:
            return f"Failed to connect to MCP server {url!r}: {exc}"
        note = (
            ""
            if result.persisted
            else " (session only; add it to config.toml [mcp] to keep a bearer-token server across restarts)"
        )
        names = ", ".join(result.tool_names) if result.tool_names else "(no new tools)"
        # A tool result, so it says what the model can do next, not why.
        return f"Connected to {url}. Its tools: {names}. {_REACH}{note}"

    @tool
    async def remove_mcp_server(url: str) -> str:
        """Disconnect a remote MCP server added earlier and stop offering its tools.

        Closes the connection and forgets the server, so it is not reconnected on the next restart.
        A sub-agent spawned from then on will not have its tools. Pass the same URL used to add it.
        """
        try:
            result = await servers.remove_server(
                connections,
                url,
                config_path=config_path,
                for_each_agent=for_each_agent,
                refresh_workers=refresh_workers,
            )
        except NotConnected:
            return f"No MCP server is connected at {url!r}."
        names = ", ".join(result.freed_tool_names) if result.freed_tool_names else "(none)"
        # The mirror of the connect message, and wrong in the same way if it claimed the tools are gone
        # now: a live agent's tool list is not rewritten either, so the change shows up in the next
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
