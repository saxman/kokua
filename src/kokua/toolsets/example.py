"""A tiny example toolset, the template for third-party tool modules.

It contributes one trivial tool so the plugin path is real and testable end to end. Copy this
shape into your own package, register it under the ``kokua.toolsets`` entry-point group, and
``pip install`` it: Kokua will discover the toolset and add its tools to the agent automatically.
"""

from __future__ import annotations

import random

from aimu.tools import tool

from kokua.config import AssistantConfig
from kokua.toolsets import Toolset


def build(config: AssistantConfig) -> list:
    """Return this toolset's tools. Receives the config in case a toolset needs to read it."""

    @tool
    def roll_dice(sides: int = 6) -> str:
        """Roll a single die and return the result.

        Args:
            sides: Number of sides on the die (default 6).
        """
        if sides < 1:
            return "A die needs at least 1 side."
        return f"Rolled a {random.randint(1, sides)} (d{sides})."

    return [roll_dice]


TOOLSET = Toolset(
    name="example",
    description="A demonstration toolset (a dice roller) showing how to add tools as a plugin.",
    build=lambda ctx: build(ctx.config),
)
