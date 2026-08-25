"""Authoring skills, and attaching runnable scripts to them, wrapped as a toolset.

Defines no tools of its own: it hands an agent AIMU's ``author_skill`` and ``add_skill_script``. Note
what it does *not* add. Kokua builds its entry agent as an ``aio.SkillAgent``, so AIMU already gives that
agent the skill catalogue, ``activate_skill``, and one tool per skill script whether or not this toolset
is declared. Declaring it adds the two authoring tools, and nothing else.

``entry_point_only`` is not a policy choice here, unlike anywhere else it could be used, and the reason
it has to be enforced at *declaration* is worth knowing. A spawned worker is a plain ``aio.Agent`` and
reaches ``build`` with ``ctx.agent`` as ``None``. That builds fine: ``make_skill_script_tool`` uses its
agent at call time, for ``await agent.reload_skills()``, so the tool constructs quietly and then fails
mid-call, *after* writing the script to disk. Nothing at build time could catch it, which is why
``select`` refuses the declaration at startup instead.

Holding this toolset also opts an agent out of catalogue scoping (see ``LiveState.skill_manager``): an
author has to see the skill it just wrote, since ``add_skill_script`` tells the model its script is
callable in the same turn, which cannot be true if the new skill falls outside an ``include`` set fixed
at startup.
"""

from __future__ import annotations

from aimu.skills import make_skill_authoring_tool, make_skill_script_tool

from kokua.registry import Toolset

GUIDANCE = (
    " When the user teaches you a repeatable procedure worth remembering, call `author_skill` to save "
    "it as a reusable skill; name skills in kebab-case (lowercase words joined by hyphens, e.g. "
    "'weekly-review'), never with underscores or spaces. When a procedure can be automated, call "
    "`add_skill_script` to attach a runnable Python or shell script to a skill; the script becomes a "
    "tool you can run immediately, even in the same turn. If a script fails, fix it by calling "
    "`add_skill_script` again with the SAME filename to overwrite it (a different filename just "
    "creates a duplicate and leaves the broken script). Scripts run with full access to this "
    "machine, so only automate what the user asked for."
)

TOOLSET = Toolset(
    name="skills",
    description="Author skills and attach runnable scripts to them.",
    build=lambda ctx: [
        make_skill_authoring_tool(ctx.state.skill_manager, ctx.config.skills_dir),
        make_skill_script_tool(ctx.agent, ctx.state.skill_manager, ctx.config.skills_dir),
    ],
    guidance=GUIDANCE,
    cross_cutting=True,
    entry_point_only=True,
)
