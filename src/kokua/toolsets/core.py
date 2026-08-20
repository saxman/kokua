"""The six capabilities Kokua itself defines, collected for the registry.

Each is declared in the sibling module that wraps its subsystem, so this file is an index rather than a
second declaration that could drift from the first. ``planning`` is one of them even though it carries a
workflow instead of tools: a turn strategy is resolved through the same registry and declared in the
same ``[agents.<name>].tools`` list as everything else here.
"""

from kokua.toolsets.capabilities import TOOLSET as CAPABILITIES_TOOLSET
from kokua.toolsets.config import TOOLSET as CONFIG_TOOLSET
from kokua.toolsets.conversations import TOOLSET as CONVERSATIONS_TOOLSET
from kokua.toolsets.mcp_admin import TOOLSET as MCP_TOOLSET
from kokua.toolsets.planning import TOOLSET as PLANNING_TOOLSET
from kokua.toolsets.scheduling import TOOLSET as SCHEDULING_TOOLSET
from kokua.toolsets.registry import Toolset

CORE_TOOLSETS: tuple[Toolset, ...] = (
    CAPABILITIES_TOOLSET,
    CONFIG_TOOLSET,
    CONVERSATIONS_TOOLSET,
    MCP_TOOLSET,
    PLANNING_TOOLSET,
    SCHEDULING_TOOLSET,
)
