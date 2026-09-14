"""AIMU's filesystem writes, wrapped as a toolset of their own.

Defines no tools of its own: an agent declaring ``fs_write`` gets ``write_file`` and ``edit_file``.
Neither confines a write to any root, deliberately and by AIMU's own design (a root check a symlink or
a relative path can walk out of reads as containment while providing none), so this reaches any path the
account running Kokua can write. Both are in the shipped ``[security].confirm_tools`` for that reason,
alongside ``execute_python`` and ``run_command``.

This is a second toolset rather than two more tools in ``fs`` because one name cannot say both things.
AIMU 0.31.0 put the writers *into* ``builtin.fs``, and Kokua's config has exactly one lever for a
capability: an agent's ``tools`` list names a toolset or it does not. With a single ``fs`` there is no
way to write down "read a file and do not write one", which is what the shipped ``introspector`` needs
and what the shipped ``coder`` does not.

The split is also what keeps the approval gate meaningful. ``[security].confirm_tools`` gates
``execute_python`` and ``run_command`` because they write with this process's privileges; an ungated
``write_file`` in the same agent would be a shorter route to the same outcome, so the gate would have
been approving the long way round and waving the short one through.

``select(builtin.fs, include=builtin.unscoped)`` is the complement of what ``toolsets/fs.py`` selects,
computed from the same two AIMU names rather than from a list of tools spelled here, so the two
toolsets partition the group by reach and cannot drift apart or overlap when AIMU adds to it.

No ``guidance``, and the foot-gun here is real enough to say why not. Replacing a whole file to change
one line destroys everything the model did not include, which is exactly the kind of trigger guidance
exists for -- except that AIMU's own ``write_file`` schema already carries it ("This replaces the whole
file. To change part of one, use edit_file"), and a second copy in the system prompt would be paid for
on every request to say what the tool says when the model reads it. ``web`` remains the one AIMU group
Kokua adds guidance to, because its trigger is epistemic (a model that believes it knows the answer
never reaches for a search tool) rather than written on the tool.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


TOOLSET = Toolset(
    name="fs_write",
    description="Create, replace, and edit files on this machine.",
    build=lambda ctx: builtin.select(builtin.fs, include=builtin.unscoped),
)
