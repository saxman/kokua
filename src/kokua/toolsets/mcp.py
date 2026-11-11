"""The ``mcp`` toolset: ``add_mcp_server`` / ``remove_mcp_server`` over ``kokua.mcp.servers``.

The connection machinery is in ``mcp/servers.py``; everything here is what the model reads. That split
matters most for the docstrings below, which are long because they are load-bearing: both tools have to
tell the model exactly how far a runtime change reaches, or it promises the user a capability it will
not have this session.

Two separate things bound when a change takes effect. Distinguish the cases, because only one of them
is reachable in-process:

* **A server already declared in ``[[mcp.server]]`` at startup** (possibly unreachable then, or removed
  earlier this session) has a name in the toolset registry, so an agent's ``available`` may already
  reference it, and an ``available = "all"`` agent already holds it. Connecting or disconnecting it
  reaches the *next* sub-agent spawned by such an agent, because ``refresh_workers`` rebuilds that
  agent's ``spawn_subagent`` and the specs re-resolve against the live connections. It does not reach a
  live agent's *own* tool list, which ``wire_agent`` built once; that waits for the agent to be rebuilt
  (a new conversation, an LRU eviction, a restart).
* **A genuinely new server reaches nothing at all until a restart,** and then only an agent that
  declares ``available = "all"`` holds it without being told its name; a list agent needs a human to
  name it too. ``build_registry`` runs once, in ``Assistant.create``, over ``config.mcp_servers`` as
  loaded at startup, and the one mid-session refresh (``core.build.refresh_skills``) re-reads skills
  only, so a new server's name is not a key in it. An ``[agents.*]`` edit *may* name the server, because
  ``update_config``'s dry run rereads the file and so sees the ``[[mcp.server]]`` entry written here, but
  that declaration resolves against no live registry either: it, too, is for the next start.
  ``add_mcp_server`` therefore persists the server for the *next* start and connects it for nothing
  else. This is the dominant flow, and both tool results say so.

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
from kokua.registry.registry import Toolset

logger = logging.getLogger(__name__)

# The qualifier that is easy to lose and load-bearing: the toolset registry is built once in
# Assistant.create from the config as loaded then, so a server that was not already a [[mcp.server]]
# entry at startup has no name in it. An [agents.*] edit may declare the new server, since
# update_config's dry run rereads the file, but nothing in this process resolves that declaration.
# Only a restart makes a new server referable, and only a persisted one comes back at all. The one
# exception is a server config.toml declared at startup that was unreachable then (or removed since): its
# name is still in the registry, so this connection reaches a worker naming it, or declaring "all", on
# its next spawn. This result cannot tell the two apart, so it states both.
_REACH = (
    "You cannot call these yourself. If this server was already in config.toml when Kokua started, a "
    'sub-agent you spawn that names it in its available list, or declares available = "all", can use '
    "them from its next spawn. Otherwise nothing can use them this session: after Kokua restarts, an agent "
    'declaring available = "all" can, and any other agent once the user names the server in its available '
    "list."
)
# A bearer-token server is never written to config.toml, so a restart drops it, and a server new to this
# session has no name in the registry, so nothing can name it. (A startup-declared server whose token_env
# was unset at startup, then added here with a bearer token, is the rare exception: its name is registered.)
_REACH_SESSION_ONLY = (
    "You cannot call these yourself, and no agent can name a server added at runtime, so nothing can use "
    "it. The connection lasts this session only, and its token is not saved, so no restart brings it back."
)
_KEEP_BEARER = (
    " To keep it, the user adds an [[mcp.server]] entry with url, name and token_env (the name of an "
    "environment variable holding the token), names that server in an agent's available list (or uses an "
    '"all" agent), and restarts Kokua.'
)


def make_mcp_tools(
    for_each_agent,
    connections: list,
    *,
    notify,
    oauth,
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
        at startup and that agent names it or declares available = "all"; a server new to this session is
        usable only after a restart, and only if it was remembered (see above): by an agent declaring
        available = "all" or one the user edits config.toml to name. Do not promise to use it in this
        conversation.

        Authentication is handled for you: just pass the URL. If the server is unprotected it
        connects directly. If it requires OAuth, you post an authorization link into the chat and
        open a browser window for the user to approve; once they do, the connection completes and
        the token is saved for future sessions. Do not claim you cannot authenticate or that this
        is impossible from here, that flow is built in. Pass bearer_token only when the user gives
        you a static token to use instead of the OAuth flow.

        Some servers require authentication but do not support the automatic OAuth flow. When that
        happens this returns a message asking for a bearer token: relay it, ask the user for a
        token, then call this tool again with that bearer_token.

        When a server is recorded, its name is derived from the URL, but that name reaches an
        agent declaring available = "all" at the next restart, if the server was remembered, and any other
        agent only once a human adds it to that agent's available list in config.toml's [agents.*.tools]
        table AND restarts Kokua: that section is locked by default and is read only at startup, so this
        tool cannot grant a named agent the capability it just connected.
        """
        try:
            result = await servers.add_server(
                connections,
                url,
                bearer_token=bearer_token,
                notify=notify,
                oauth=oauth,
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
        names = ", ".join(result.tool_names) if result.tool_names else "(no new tools)"
        # A tool result, so it says what the model can do next, not why.
        reach = _REACH if result.persisted else _REACH_SESSION_ONLY + _KEEP_BEARER
        return f"Connected to {url}. Its tools: {names}. {reach}"

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
        oauth=ctx.state.oauth,
        config_path=ctx.config.config_path,
        refresh_workers=ctx.state.refresh_workers,
    )


TOOLSET = Toolset(
    name="mcp",
    description="Connect and disconnect remote MCP servers at runtime.",
    build=_build,
    cross_cutting=True,
)
