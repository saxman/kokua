"""Remote MCP servers: connecting them, authenticating them, and adding them at runtime.

The agent tools that drive this live in ``kokua.toolsets.mcp_admin``; nothing here formats a sentence.
"""

from .auth import build_chat_oauth
from .servers import (
    AlreadyConnected,
    BearerTokenRequired,
    ConnectFailed,
    NotConnected,
    ServerAdded,
    ServerConnection,
    ServerRemoved,
    add_server,
    attach_server,
    connect_mcp,
    reconnect_mcp_servers,
    remove_server,
)

__all__ = [
    "ServerConnection",
    "ServerAdded",
    "ServerRemoved",
    "BearerTokenRequired",
    "AlreadyConnected",
    "NotConnected",
    "ConnectFailed",
    "connect_mcp",
    "attach_server",
    "add_server",
    "remove_server",
    "reconnect_mcp_servers",
    "build_chat_oauth",
]
