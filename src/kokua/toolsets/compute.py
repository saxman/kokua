"""AIMU's Python, shell, and calculation tools, wrapped as a toolset.

Defines no tools of its own. Note what declaring this grants: ``execute_python`` and its siblings run
with the privileges of the Kokua process, which is why the shipped ``[security].confirm_tools`` gates
``execute_python`` by name rather than leaving the agent's declaration as the only control.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="compute",
    description="Run Python, shell commands, and calculations.",
    build=lambda ctx: list(builtin.compute),
)
