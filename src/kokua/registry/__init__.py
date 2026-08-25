"""The toolset registry: the one namespace every capability an agent can declare resolves through.

A ``Toolset`` is the single shape every capability takes, whether it is one of Kokua's own, a wrapper
over an AIMU tool group, an installed third party's, or a configured MCP server. That is what lets an
agent's ``tools`` list name a capability without naming what kind of thing provides it.

This package holds the machinery and no toolsets: the ``Toolset`` and ``Setting`` types, name resolution
(``select``, ``build_tools``, ``register``), and the live state a ``build`` draws on (``LiveState``,
``ToolsetContext``). The toolsets themselves are one file each under :mod:`kokua.toolsets`, which holds
nothing but toolsets, so neither directory makes a reader sort one kind of file from the other.
Assembling the namespace from every provider is :mod:`kokua.core.agents`, which needs both.
"""

from kokua.registry.context import LiveState, ToolsetContext
from kokua.registry.registry import (
    Setting,
    Toolset,
    ToolsetError,
    ToolsetRegistry,
    build_tools,
    register,
    select,
    workflows_of,
)

__all__ = [
    "LiveState",
    "Setting",
    "Toolset",
    "ToolsetContext",
    "ToolsetError",
    "ToolsetRegistry",
    "build_tools",
    "register",
    "select",
    "workflows_of",
]
