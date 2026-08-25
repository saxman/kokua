"""AIMU's speech-to-text, wrapped as a toolset.

Defines no tools of its own. Needs ``AIMU_TRANSCRIPTION_MODEL`` set and raises at call time otherwise,
so an agent declaring this is opting into that requirement.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="transcription",
    description="Speech to text. Needs AIMU_TRANSCRIPTION_MODEL.",
    build=lambda ctx: list(builtin.transcription),
)
