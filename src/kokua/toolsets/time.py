"""AIMU's clock and timezone tools, wrapped as a toolset.

Defines no tools of its own. Marked ``cross_cutting`` even though it is an AIMU group like ``fs``,
because that flag asks what an agent holds a capability *for*: an agent keeps a clock for its own
scheduling and "when" questions, so holding one does not make it a domain worker, and a lean supervisor
declaring only this still reads as lean to the delegation guidance.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="time",
    description="The current date and time, and timezone conversion.",
    build=lambda ctx: list(builtin.time),
    cross_cutting=True,
)
