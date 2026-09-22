"""Authoring skills, revising them, and attaching runnable scripts to them, wrapped as a toolset.

Defines no tools of its own: it hands an agent AIMU's ``author_skill``, ``update_skill``, and
``add_skill_script``. The three cover one loop, and the middle one is the reason the loop closes:
``author_skill`` refuses to clobber, so without an update path a skill's prose was write-once, and an
agent could fix a skill's scripts forever while the instructions a first attempt most often gets wrong
stayed as written. Note what this toolset does *not* add. Kokua builds its entry agent as an ``aio.SkillAgent``, so AIMU already gives that
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
at startup. ``update_skill`` needs the same thing for a different reason: a skill it cannot see is one
it reports as not found.

Of the three, the shipped ``[security].confirm_tools`` gates ``add_skill_script`` alone. A gate is for a
call that reaches past the model, which a script does (it runs as a real subprocess with the user's
privileges) and prose does not, and ``update_skill`` is held to the same line as ``author_skill`` for
that reason: gating the tool that edits instructions while leaving ungated the one that writes them
would buy a prompt and no boundary. Add either by hand if you want the prompt.
"""

from __future__ import annotations

from aimu.skills import make_skill_authoring_tool, make_skill_script_tool, make_skill_update_tool

from kokua.registry import Toolset

GUIDANCE = (
    " When the user teaches you a repeatable procedure worth remembering, call `author_skill` to save "
    "it as a reusable skill; name skills in kebab-case (lowercase words joined by hyphens, e.g. "
    "'weekly-review'), never with underscores or spaces. When a skill turns out to be wrong or "
    "incomplete, call `update_skill` to fix its instructions in place rather than authoring a second "
    "skill for the same job; pass only the part you are changing. When a procedure can be automated, "
    "call `add_skill_script` to attach a runnable Python or shell script to a skill; the script becomes "
    "a tool you can run immediately, even in the same turn. If a script fails, fix it by calling "
    "`add_skill_script` again with the SAME filename to overwrite it (a different filename just "
    "creates a duplicate and leaves the broken script). Scripts run with full access to this "
    "machine, so only automate what the user asked for."
)

TOOLSET = Toolset(
    name="skills",
    description="Author skills, revise them, and attach runnable scripts to them.",
    build=lambda ctx: [
        make_skill_authoring_tool(ctx.state.skill_manager, ctx.config.skills_dir),
        make_skill_update_tool(ctx.state.skill_manager, ctx.config.skills_dir),
        make_skill_script_tool(ctx.agent, ctx.state.skill_manager, ctx.config.skills_dir),
    ],
    guidance=GUIDANCE,
    cross_cutting=True,
    entry_point_only=True,
)
