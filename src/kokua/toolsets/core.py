"""The four capabilities Kokua itself defines, collected for the registry.

Each is declared in the sibling module that wraps its subsystem, so this file is an index rather than a
second declaration that could drift from the first.
"""

from kokua.config.tools import TOOLSET as CONFIG_TOOLSET
from kokua.core.tools import TOOLSET as CONVERSATIONS_TOOLSET
from kokua.mcp.tools import TOOLSET as MCP_TOOLSET
from kokua.toolsets.scheduling import TOOLSET as SCHEDULING_TOOLSET
from kokua.toolsets.registry import Toolset

CORE_TOOLSETS: tuple[Toolset, ...] = (
    CONFIG_TOOLSET,
    CONVERSATIONS_TOOLSET,
    MCP_TOOLSET,
    SCHEDULING_TOOLSET,
)
