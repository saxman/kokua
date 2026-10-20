"""AIMU's clock and timezone tools, wrapped as a toolset.

Defines no tools of its own. Marked ``cross_cutting`` even though it is an AIMU group like ``fs``,
because that flag asks what an agent holds a capability *for*: an agent keeps a clock for its own
scheduling and "when" questions, so holding one does not make it a domain worker, and a lean supervisor
declaring only this still reads as lean to the delegation guidance.

It carries guidance for the reason ``web`` does: a model believes it knows what day it is. Its sense of
the present is its training data, so without a sentence saying otherwise it answers "how long until"
from a date it never read, and converts a timezone by adding an offset it remembers, which is wrong for
half the year wherever daylight saving applies. The schema says what the tools do, never that the
model's own sense of the clock is the thing not to trust.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


GUIDANCE = (
    " Your sense of the current date and time comes from training, not from a clock. Call "
    "`get_current_date_and_time` before anything that depends on the present moment (today's date, how "
    "long until or since something, whether a time has passed), and use `convert_time` for a timezone "
    "conversion rather than adding or subtracting an offset yourself."
)


TOOLSET = Toolset(
    name="time",
    description="The current date and time, and timezone conversion.",
    build=lambda ctx: list(builtin.time),
    guidance=GUIDANCE,
    cross_cutting=True,
)
