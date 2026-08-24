"""The ``capabilities`` toolset: the registry an agent can read, and a sub-agent it can compose from it.

No registry is defined here. ``ToolsetRegistry`` already indexes every capability by name with a
description and a provider label, and reaches this module as ``ctx.state.registry``; what was missing
was a view of it a model can read and a way to act on what the view shows.

Discovery never calls ``Toolset.build``. Building has real side effects -- ``memory`` instantiates a
``SemanticMemoryStore`` and loads an embedding model, an MCP toolset touches live connections, a
plugin's build may fail -- and none of them should be paid to answer a question about what exists. That
is also why ``Toolset`` grows no field listing its tool names: a second declaration of what a toolset
contains could drift from what ``build`` returns, and the description is what the model picks by
anyway, exactly as it picks a skill from its catalogue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Mapping

from aimu.tools import tool

from kokua.toolsets.registry import Setting, Toolset, ToolsetError, build_tools, select

if TYPE_CHECKING:
    from kokua.config.schema import AssistantConfig
    from kokua.toolsets.context import LiveState, ToolsetContext

#: The registry name this toolset is installed under. Defined once so `TOOLSET` and the guard in
#: `_compose_spec` that refuses to hand a composed sub-agent this name cannot drift apart.
TOOLSET_NAME = "capabilities"

DEFAULT_MAX_DEPTH = 3

#: The ``[capabilities]`` section of config.toml, owned here rather than by ``AssistantConfig``. Hot
#: because a runaway composition is something a user wants to rein in mid-session, without a restart.
CAPABILITIES_SETTINGS: tuple[Setting, ...] = (Setting("max_depth", int, DEFAULT_MAX_DEPTH, hot=True),)

DEFAULT_SUBAGENT_NAME = "composed"

DEFAULT_SUBAGENT_INSTRUCTIONS = (
    "You are a sub-agent composed for one task. Complete it with the tools you have been given and "
    "return a single, complete answer."
)

# Prefixed so the string can never equal an agent name from config.toml. `select` rejects an
# entry_point_only toolset by comparing its `agent` argument to the entry point, and the sub-agent's
# name is chosen by the model: an unprefixed "assistant" would resolve `skills` for it and then reach
# `build` with agent=None, where the skill script tool needs a live agent. A colon cannot appear in a
# TOML bare key, so the two namespaces cannot meet.
_SELECT_LABEL = "composed:{name}"


def _compose_spec(
    name: str,
    tools: list[str],
    instructions: str,
    state: "LiveState",
    *,
    extra_tools: list | None,
    model: str | None,
) -> dict:
    """The AIMU ``agent_types`` spec for one composed sub-agent.

    Raises ``ToolsetError`` when a name does not resolve, which the calling tool turns into text rather
    than letting it propagate: a raising tool breaks the agent's tool loop. Naming this toolset itself
    among ``tools`` is one such rejection, checked before ``select`` runs so the message is specific
    rather than a generic resolution error: it is also the escape hatch that would defeat the depth
    cap, since a sub-agent handed a fresh ``compose_subagent`` of its own would read the cap again from
    scratch instead of spending down the budget the caller already holds. That check strips and
    lowercases each name, because a variant spelling falling through to ``select`` would be answered
    with a list of available toolsets that names ``capabilities``, re-advertising the one name this
    refuses.

    ``extra_tools`` is placed, not built. Whether a composed sub-agent may compose another, and look
    itself up in
    the catalogue to do so, is a question about the depth budget, which belongs to the tool that owns the
    recursion; keeping it out of here means these two functions are not mutually recursive and this
    one stays a pure translation from names to a spec.
    """
    from kokua.toolsets.context import ToolsetContext

    if any(requested.strip().lower() == TOOLSET_NAME for requested in tools):
        raise ToolsetError(
            f"{TOOLSET_NAME!r} cannot be given to a composed sub-agent: how deep composition may nest is "
            "governed by [capabilities].max_depth, not by naming this toolset, and a composed sub-agent "
            "receives its own discovery and composition tools when depth remains."
        )
    config = state.config
    selected = select(tools, state.registry, agent=_SELECT_LABEL.format(name=name), entry_point=config.entry_agent)
    # agent=None is what a spawned sub-agent always gets. The one toolset needing a live agent object is
    # entry-point-only, and `select` above has already rejected it here.
    built = build_tools(selected, ToolsetContext(state=state, agent=None))
    if extra_tools:
        built = built + list(extra_tools)
    opener = instructions.strip() or DEFAULT_SUBAGENT_INSTRUCTIONS
    spec: dict = {
        "system_message": "".join([opener] + [toolset.guidance for toolset in selected if toolset.guidance]),
        "tools": built,
    }
    if model:
        spec["model"] = model
    # Resolved rather than declared, for the reason `build_agent_specs` resolves them: AIMU reads a
    # missing key as "no tier", so an undeclared agent would skip the [assistant] defaults instead of
    # inheriting them. Both accessors fall back to those defaults for a name absent from [agents.*],
    # which a composed sub-agent's always is.
    thinking = config.thinking_for(name)
    if thinking is not None:
        spec["thinking"] = thinking
    generation = config.generation_for(name)
    if generation:
        spec["generate_kwargs"] = generation
    return spec


def _catalogue(registry: Mapping[str, Toolset], filter_text: str) -> str:
    """One line per registry entry except this toolset's own, sorted by name: name, provider,
    description.

    ``providers`` is read defensively because ``LiveState.registry`` is typed as a plain dict and a
    state built by hand may carry one; a missing provider map should degrade to an unlabeled line
    rather than break discovery. This toolset's own entry is left out: the agent reading the catalogue
    already holds it, so listing it back is noise, and naming it to ``compose_subagent`` only earns a
    rejection.
    """
    providers = getattr(registry, "providers", {})
    needle = filter_text.strip().lower()
    lines = [
        f"{name} [{providers.get(name, 'unknown')}]: {registry[name].description}"
        for name in sorted(registry)
        if name != TOOLSET_NAME
        and (not needle or needle in name.lower() or needle in registry[name].description.lower())
    ]
    if not lines:
        return f"No capability matches {filter_text!r}. Call list_capabilities with no filter to see them all."
    return "\n".join(lines)


def _max_depth(config: "AssistantConfig") -> int:
    """The configured composition cap, floored at zero.

    Read from ``toolset_settings`` at call time rather than closed over at build time: ``build`` runs
    once per conversation agent, so a build-time read would leave a hot change stranded until restart.
    """
    configured = config.toolset_settings.get(TOOLSET_NAME, {}).get("max_depth", DEFAULT_MAX_DEPTH)
    return max(0, int(configured))


def _make_list_tool(state: "LiveState") -> Callable:
    """``list_capabilities``, factored out so the real agent and a composed sub-agent with depth remaining
    share one definition rather than each holding a copy that could drift.
    """

    @tool
    async def list_capabilities(filter: str = "") -> str:
        """List every capability installed on this machine other than this one, whether or not you
        currently hold it.

        Each line is a capability name, the kind of provider it came from, and what it does. Use this
        when a task needs something none of your own tools and none of your named sub-agent roles
        cover, then pass the names you need to compose_subagent.

        Args:
            filter: Optional. Show only capabilities whose name or description contains this text.
        """
        return _catalogue(state.registry, filter)

    return list_capabilities


def _make_compose_tool(state: "LiveState", *, remaining_depth: int | None, model: str | None) -> Callable:
    """``compose_subagent`` with ``remaining_depth`` compositions left to the agent holding it.

    ``None`` means "read the cap when called", which is what the tool given to a real agent is built
    with. A nested tool carries a concrete count instead, so lowering the cap mid-turn cannot extend a
    chain that is already running.

    The recursion lives here and only here. Since a composed sub-agent refers to no declared agent, there
    is no graph for ``validate_agents`` to prove acyclic, so this decrement is the whole termination
    argument, and it is a closure variable precisely so nothing the model writes can influence it. That
    is also why a composed sub-agent is never handed this toolset's own name to select on its own
    behalf: naming it would let it rebuild a fresh, full-budget ``compose_subagent``, bypassing the
    count entirely; ``_compose_spec`` refuses that name outright, and one that still has budget is
    instead handed the pair of tools directly, below.
    """

    @tool
    async def compose_subagent(name: str, task: str, tools: list[str], instructions: str) -> str:
        """Build a sub-agent with exactly the capabilities a task needs, run one task on it, and return
        its answer.

        If one of your named sub-agent roles fits, prefer spawn_subagent with that role: it costs one
        fewer step and carries instructions already written for its job. Use this when no role fits.
        Call list_capabilities first to see what the names are. The sub-agent shares no history with
        you, so the task must be self-contained, and it is discarded afterwards.

        Args:
            name: A short kebab-case label for this sub-agent, used in the logs and the sub-agent
                card, e.g. "quote-checker".
            task: The complete, self-contained task for it to carry out.
            tools: The capability names to give it, from list_capabilities, e.g. ["web", "compute"].
            instructions: Its standing instructions: its role, and how to report back.
        """
        label = name.strip() or DEFAULT_SUBAGENT_NAME
        if not tools:
            return (
                f"No capabilities named for {label!r}. Call list_capabilities to see what is installed, "
                "then pass the names this task needs."
            )
        depth = _max_depth(state.config) if remaining_depth is None else remaining_depth
        if depth <= 0:
            return "Composing a sub-agent is switched off: [capabilities].max_depth is 0. Ask the user to raise it."
        # A call spends one, so the sub-agent gets discovery and a composition tool of its own only
        # while a composition would remain after this one; the two are handed together because a
        # compose_subagent with no way to look up names is useless to whatever holds it.
        nested = _make_compose_tool(state, remaining_depth=depth - 1, model=model) if depth > 1 else None
        extra_tools = [_make_list_tool(state), nested] if nested is not None else None
        try:
            spec = _compose_spec(label, tools, instructions, state, extra_tools=extra_tools, model=model)
        except ToolsetError as error:
            return f"Could not compose {label!r}: {error}"
        # Imported here rather than at module scope because aimu.aio.tools.builtin is the AIMU surface
        # aimu_compat.require_aimu probes, and this module is reached whenever config is resolved (the
        # settings table collects what every installed toolset declares), which happens on invocations
        # that run before the preflight does. A module-scope import would turn a stale AIMU into a bare
        # ImportError on those, instead of the fix the preflight prints.
        from aimu.aio.tools.builtin import make_async_subagent_tool

        spawn = make_async_subagent_tool(
            model,
            agent_types={label: spec},
            max_depth=1,
            tool_approval=state.tool_approval,
            observer=state.observer,
        )
        return await spawn(label, task)

    return compose_subagent


def make_capability_tools(ctx: "ToolsetContext") -> list:
    state = ctx.state
    # The [assistant] default, falling back to whatever the holder's own client already resolved. That
    # is the fallback `make_delegation_tool` takes, for the same reason: with no default set, resolving
    # per composition would pick a model afresh each time instead of staying on the one in use.
    model = state.config.model or getattr(getattr(ctx.agent, "model_client", None), "model", None)
    return [_make_list_tool(state), _make_compose_tool(state, remaining_depth=None, model=model)]


# Ranked deliberately. `spawn_subagent`'s roles and `compose_subagent` are two routes to the same place,
# and a model given both with nothing to choose between them picks arbitrarily. A declared role wins by
# default because its instructions were written for its job, where a composed sub-agent's are written
# in the moment.
CAPABILITIES_GUIDANCE = (
    " You can see every capability installed on this machine, not only the ones you hold. When a task "
    "needs something you lack, first check whether one of your `spawn_subagent` roles already covers "
    "it. If none does, call `list_capabilities` to find the capability names, then `compose_subagent` to "
    "build a sub-agent holding exactly those and give it the task."
)

TOOLSET = Toolset(
    name=TOOLSET_NAME,
    description="Discover every installed capability and compose a sub-agent from the ones a task needs.",
    build=make_capability_tools,
    guidance=CAPABILITIES_GUIDANCE,
    settings=CAPABILITIES_SETTINGS,
    # Discovering and composing is how an agent manages its own work rather than a domain capability,
    # so a lean supervisor declaring only it still reads as lean to the delegation guidance.
    cross_cutting=True,
)
