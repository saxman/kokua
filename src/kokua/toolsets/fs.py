"""AIMU's filesystem reads, wrapped as a toolset.

Defines no tools of its own: an agent declaring ``fs`` gets the read members of AIMU's ``builtin.fs``
group. These read the machine Kokua runs on, so declaring this in an agent's ``tools`` is what grants
that reach.

Note what the ``build`` does not say, because it used to be ``list(builtin.fs)``. AIMU 0.31.0 added
``write_file`` and ``edit_file`` *into* that group, so handing it out unchanged would have granted a
write to every agent already declaring ``fs``, on an upgrade, with no config change and nothing
reported: the shipped ``introspector`` declares ``fs`` to read an export it was asked to evaluate, and
would have gained the ability to rewrite one. A capability is declared, never defaulted, which is why
this narrows and ``fs_write`` exists to be declared separately.

``select(..., exclude=builtin.unscoped)`` rather than a list of the two tools Kokua wants. The
exclusion is by *reach*: ``builtin.unscoped`` is AIMU's own name for the tools whose target the model
names (arbitrary code, an arbitrary command, an arbitrary path), so a read tool AIMU adds to ``fs``
later arrives here on its own, and a writer arrives in ``fs_write`` instead of here. A name list would
have to be edited for either, and the failure of forgetting differs by direction: a missed read is a
capability quietly absent, a missed writer is this toolset quietly no longer read-only.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="fs",
    description="Read files and list directories on this machine.",
    build=lambda ctx: builtin.select(builtin.fs, exclude=builtin.unscoped),
)
