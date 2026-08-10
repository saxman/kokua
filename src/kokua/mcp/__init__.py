"""Remote MCP servers: connecting them, authenticating them, and adding them at runtime."""

from .auth import build_chat_oauth
from .servers import (
    BearerTokenRequired,
    ServerConnection,
    attach_server,
    connect_mcp,
    make_mcp_tools,
    reconnect_mcp_servers,
)

__all__ = [
    "ServerConnection",
    "BearerTokenRequired",
    "connect_mcp",
    "attach_server",
    "reconnect_mcp_servers",
    "make_mcp_tools",
    "build_chat_oauth",
]
