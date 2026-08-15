"""Named capability providers, and the registry that resolves an agent's declared names to tools.

A ``Toolset`` is the one shape every capability takes: Kokua's own core capabilities, AIMU's built-in
tool groups, an installed plugin, and a configured MCP server all register here, so an agent's
declaration names a capability without naming what kind of thing provides it.

A plugin module exposes a module-level ``TOOLSET`` (a :class:`Toolset`) registered under the
``kokua.toolsets`` entry-point group. A third party publishes a package that registers its own
``kokua.toolsets`` entry point, and Kokua discovers it at runtime, merging its tools into the agent
exactly as it would one of its own.
"""

from kokua.toolsets.context import LiveState, ToolsetContext
from kokua.toolsets.registry import Toolset, ToolsetError, build_tools, register, select

__all__ = ["LiveState", "Toolset", "ToolsetContext", "ToolsetError", "build_tools", "register", "select"]
