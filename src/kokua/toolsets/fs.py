"""AIMU's filesystem tools, wrapped as a toolset.

Defines no tools of its own: an agent declaring ``fs`` gets the callables AIMU's ``builtin.fs`` group
provides. These read the machine Kokua runs on, so declaring this in an agent's ``tools`` is what grants
that reach; nothing here narrows it.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="fs",
    description="Read files and list directories on this machine.",
    build=lambda ctx: list(builtin.fs),
)
