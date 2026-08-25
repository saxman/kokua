"""AIMU's text-to-speech, wrapped as a toolset.

Defines no tools of its own. Needs ``AIMU_SPEECH_MODEL`` set and raises at call time otherwise, so an
agent declaring this is opting into that requirement.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="speech",
    description="Text to speech. Needs AIMU_SPEECH_MODEL.",
    build=lambda ctx: list(builtin.speech),
)
