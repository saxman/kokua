"""AIMU's audio generation, wrapped as a toolset.

Defines no tools of its own. Needs ``AIMU_AUDIO_MODEL`` set, and raises at call time when it is not, so
an agent declaring this is opting into that requirement. Unlike Kokua's ``image`` toolset, which offers
no tool at all without its model configured, this is offered regardless: the env var is read by AIMU at
call time rather than at build time, so there is nothing to check here that would still be true later.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="audio",
    description="Audio generation. Needs AIMU_AUDIO_MODEL.",
    build=lambda ctx: list(builtin.audio),
)
