"""AIMU's assorted utility tools, wrapped as a toolset.

Defines no tools of its own. The contents are whatever AIMU's ``builtin.misc`` group holds, which is
deliberately unenumerated here: pinning the list in a docstring would go stale against the library on a
release Kokua did not change.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="misc",
    description="Assorted utilities.",
    build=lambda ctx: list(builtin.misc),
)
