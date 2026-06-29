"""A tiny example tool-pack, the template for third-party tool modules.

It contributes one trivial tool so the plugin path is real and testable end to end. Copy this
shape into your own package, register it under the ``mopai.tools`` entry-point group, and
``pip install`` it: Mopai will discover the pack and add its tools to the agent automatically.
"""

from __future__ import annotations

import random

from aimu.tools import tool

from ..config import AssistantConfig
from ..plugins import ToolPack


def build(config: AssistantConfig) -> list:
    """Return this pack's tools. Receives the config in case a pack needs to read it."""

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


TOOL_PACK = ToolPack(
    name="example",
    description="A demonstration tool-pack (a dice roller) showing how to add tools as a plugin.",
    build=build,
)
