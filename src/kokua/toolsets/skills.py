"""Authoring skills, revising them, and attaching runnable scripts to them, wrapped as a toolset.

Defines no tools of its own: it hands an agent AIMU's ``author_skill``, ``update_skill``, and
``add_skill_script``. The three cover one loop, and the middle one is the reason the loop closes:
``author_skill`` refuses to clobber, so without an update path a skill's prose was write-once, and an
agent could fix a skill's scripts forever while the instructions a first attempt most often gets wrong
stayed as written. Note what this toolset does *not* add. Kokua builds its entry agent as an ``aio.SkillAgent``, so AIMU already gives that
agent the skill catalogue, ``activate_skill``, and one tool per skill script whether or not this toolset
is declared. Declaring it adds the three authoring tools (``author_skill``, ``update_skill``,
``add_skill_script``), and nothing else.

Each tool is wrapped in two ways and keeps AIMU's name and schema. Every write is followed by
``LiveState.refresh_skills``, which is what lets a skill written mid-session reach an ``available = "all"``
worker on its next spawn and ``compose_subagent`` on its next call: AIMU refreshes only the entry agent's
own catalogue, and a worker resolves skills through Kokua's registry instead. And ``author_skill``
refuses a name another toolset already holds, since skills share the one namespace every toolset does.

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

Of the three, the shipped ``[assistant.tools]`` ``confirm`` list gates ``add_skill_script`` alone. A gate is for a
call that reaches past the model, which a script does (it runs as a real subprocess with the user's
privileges) and prose does not, and ``update_skill`` is held to the same line as ``author_skill`` for
that reason: gating the tool that edits instructions while leaving ungated the one that writes them
would buy a prompt and no boundary. Add either by hand if you want the prompt.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Callable, Optional

from aimu.skills import make_skill_authoring_tool, make_skill_script_tool, make_skill_update_tool

from kokua.config import store as config_store
from kokua.config.file import ConfigError
from kokua.registry import Toolset
from kokua.registry.registry import ALL_DECLARATION, RESERVED_GATE_NAMESPACE, ToolsetError

if TYPE_CHECKING:
    from kokua.registry.context import LiveState, ToolsetContext

logger = logging.getLogger(__name__)

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


def _claimed_by(name: str, state: "LiveState") -> Optional[str]:
    """What already holds ``name`` as a toolset, or None when a skill may take it.

    Two sources, because the live registry is not everything the next start registers: a server
    ``add_mcp_server`` connected this session is written to ``config.toml`` and never reaches the registry
    until a restart, and a skill holding its name would stop that restart on the collision.

    A name that is already a skill is left to AIMU, which refuses to clobber one with a message of its own.
    """
    # Function-scope for the reason capabilities.py gives: core/build.py reaches toolsets/, so a module-scope
    # import of core.agents would close that cycle.
    from kokua.core.agents import is_skill

    if name in (RESERVED_GATE_NAMESPACE, ALL_DECLARATION):
        return "reserved"
    if name in state.registry and not is_skill(name, state.registry):
        return state.registry.providers[name]
    try:
        persisted = config_store.mcp_server_names(state.config.config_path)
    except (ConfigError, OSError, ValueError) as error:
        # The next start will refuse a file that cannot be read anyway, and refusing every skill name
        # until it is fixed would take authoring away for no protection, so the registry check stands alone.
        logger.warning("Could not read MCP server names from %s: %s", state.config.config_path, error)
        return None
    # The provider label build_registry gives an MCP server, which is the word a collision names.
    return "MCP server" if name in persisted else None


def _refusing_toolset_names(author_skill: Callable, state: "LiveState") -> Callable:
    """``author_skill``, refusing a name some other toolset already holds before anything is written.

    AIMU cannot know Kokua's namespace, so without this a skill named ``web`` is written to disk and then
    refused by the registry at the next start, as a collision that stops Kokua from starting at all. It
    wraps the refreshing tool rather than the other way round, so a refusal writes nothing and refreshes
    nothing.
    """

    @functools.wraps(author_skill)
    async def guarded(name: str, description: str, body: str) -> str:
        provider = _claimed_by(name, state)
        if provider is not None:
            return (
                f"Nothing was written: {name!r} is already a toolset name ({provider}). "
                "Choose another name for the skill."
            )
        return await author_skill(name=name, description=description, body=body)

    return guarded


def _refreshing(tool_fn: Callable, state: "LiveState", *, tools_changed: bool) -> Callable:
    """``tool_fn``, followed by ``state.refresh_skills`` so workers resolve the skills now on disk.

    ``tools_changed`` is whether the tool can change which tools a skill offers. Authoring adds a skill
    (and with it an ``activate_skill`` entry) and a script adds a tool, so both rebuild the skill tools;
    ``update_skill`` edits prose only, so it refreshes descriptions and guidance and leaves the tools,
    and with them the skills server, as they are.

    It refreshes whatever the tool answered, since a refresh after a write that did not happen costs one
    directory scan and changes nothing. A refresh that fails goes back to the model with the result rather
    than raising, because the write it follows has already happened and an error in its place would say
    otherwise. The expected failure is a collision the name check could not see (a skill directory made
    by hand, say), which the next start will refuse as well; anything else, an approval entry refusing a
    skill's new tools included, is logged with its traceback and reported the same way.
    ``functools.wraps`` copies the ``__tool_spec__`` AIMU reads, so the model sees AIMU's own schema.
    """

    @functools.wraps(tool_fn)
    async def refreshed(*args, **kwargs) -> str:
        result = await tool_fn(*args, **kwargs)
        if state.refresh_skills is None:
            return result
        try:
            state.refresh_skills(tools_changed=tools_changed)
        except ToolsetError as error:
            return f"{result}\n\nWorkers were not given the change, and the next start will refuse it too: {error}"
        except Exception as error:
            logger.warning("Refreshing the skills after %s failed.", tool_fn.__name__, exc_info=True)
            return f"{result}\n\nThe write happened, but workers were not given the change: {error}"
        return result

    return refreshed


def _build(ctx: "ToolsetContext") -> list:
    state = ctx.state
    skills_dir = ctx.config.skills_dir
    author = _refreshing(make_skill_authoring_tool(state.skill_manager, skills_dir), state, tools_changed=True)
    update = _refreshing(make_skill_update_tool(state.skill_manager, skills_dir), state, tools_changed=False)
    script = make_skill_script_tool(ctx.agent, state.skill_manager, skills_dir)
    return [
        _refusing_toolset_names(author, state),
        update,
        _refreshing(script, state, tools_changed=True),
    ]


TOOLSET = Toolset(
    name="skills",
    description="Author skills, revise them, and attach runnable scripts to them.",
    build=_build,
    guidance=GUIDANCE,
    cross_cutting=True,
    entry_point_only=True,
)
