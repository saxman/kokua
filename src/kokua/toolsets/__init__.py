"""Named capability providers, and the registry that resolves an agent's declared names to tools.

A ``Toolset`` is the one shape every capability takes: Kokua's own core capabilities, AIMU's built-in
tool groups, an installed plugin, and a configured MCP server all register here, so an agent's
declaration names a capability without naming what kind of thing provides it.
"""

from kokua.toolsets.registry import Toolset, ToolsetError, build_tools, register, select

__all__ = ["Toolset", "ToolsetError", "build_tools", "register", "select"]
