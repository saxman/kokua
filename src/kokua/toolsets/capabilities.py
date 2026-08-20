"""The ``capabilities`` toolset: the registry an agent can read, and a worker it can compose from it.

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

from aimu.aio.tools.builtin import make_async_subagent_tool
from aimu.tools import tool

from kokua.toolsets.registry import Setting, Toolset, ToolsetError, build_tools, select

if TYPE_CHECKING:
    from kokua.toolsets.context import LiveState, ToolsetContext

DEFAULT_MAX_DEPTH = 3

#: The ``[capabilities]`` section of config.toml, owned here rather than by ``AssistantConfig``. Hot
#: because a runaway composition is something a user wants to rein in mid-session, without a restart.
CAPABILITIES_SETTINGS: tuple[Setting, ...] = (Setting("max_depth", int, DEFAULT_MAX_DEPTH, hot=True),)

DEFAULT_WORKER_NAME = "composed"

DEFAULT_WORKER_INSTRUCTIONS = (
    "You are a sub-agent composed for one task. Complete it with the tools you have been given and "
    "return a single, complete answer."
)

# Prefixed so the string can never equal an agent name from config.toml. `select` rejects an
# entry_point_only toolset by comparing its `agent` argument to the entry point, and the worker's name
# is chosen by the model: an unprefixed "assistant" would resolve `skills` for a worker and then reach
# `build` with agent=None, where the skill script tool needs a live agent. A colon cannot appear in a
# TOML bare key, so the two namespaces cannot meet.
_SELECT_LABEL = "composed:{name}"


def _compose_spec(
    name: str,
    tools: list[str],
    instructions: str,
    state: "LiveState",
    *,
    compose_tool: Callable | None,
    model: str | None,
) -> dict:
    """The AIMU ``agent_types`` spec for one composed worker.

    Raises ``ToolsetError`` when a name does not resolve, which the calling tool turns into text rather
    than letting it propagate: a raising tool breaks the agent's tool loop.

    ``compose_tool`` is placed, not built. Whether a worker may compose another is a question about the
    depth budget, which belongs to the tool that owns the recursion; keeping it out of here means these
    two functions are not mutually recursive and this one stays a pure translation from names to a spec.
    """
    from kokua.toolsets.context import ToolsetContext

    config = state.config
    selected = select(tools, state.registry, agent=_SELECT_LABEL.format(name=name), entry_point=config.entry_agent)
    # agent=None is what a spawned worker always gets. The one toolset needing a live agent object is
    # entry-point-only, and `select` above has already rejected it here.
    built = build_tools(selected, ToolsetContext(state=state, agent=None))
    if compose_tool is not None:
        built = built + [compose_tool]
    opener = instructions.strip() or DEFAULT_WORKER_INSTRUCTIONS
    spec: dict = {
        "system_message": "".join([opener] + [toolset.guidance for toolset in selected if toolset.guidance]),
        "tools": built,
    }
    if model:
        spec["model"] = model
    # Resolved rather than declared, for the reason `build_agent_specs` resolves them: AIMU reads a
    # missing key as "no tier", so an undeclared worker would skip the [assistant] defaults instead of
    # inheriting them. Both accessors fall back to those defaults for a name absent from [agents.*],
    # which a composed worker's always is.
    thinking = config.thinking_for(name)
    if thinking is not None:
        spec["thinking"] = thinking
    generation = config.generation_for(name)
    if generation:
        spec["generate_kwargs"] = generation
    return spec


def _catalogue(registry: Mapping[str, Toolset], filter_text: str) -> str:
    """One line per registry entry: name, provider, description, sorted by name.

    ``providers`` is read defensively because ``LiveState.registry`` is typed as a plain dict and a
    state built by hand may carry one; a missing provider map should degrade to an unlabeled line
    rather than break discovery.
    """
    providers = getattr(registry, "providers", {})
    needle = filter_text.strip().lower()
    lines = [
        f"{name} [{providers.get(name, 'unknown')}]: {registry[name].description}"
        for name in sorted(registry)
        if not needle or needle in name.lower() or needle in registry[name].description.lower()
    ]
    if not lines:
        return f"No capability matches {filter_text!r}. Call list_capabilities with no filter to see them all."
    return "\n".join(lines)


def _max_depth(config) -> int:
    """The configured composition cap, floored at zero.

    Read from ``toolset_settings`` at call time rather than closed over at build time: ``build`` runs
    once per conversation agent, so a build-time read would leave a hot change stranded until restart.
    """
    configured = config.toolset_settings.get("capabilities", {}).get("max_depth", DEFAULT_MAX_DEPTH)
    return max(0, int(configured))


def _make_compose_tool(state: "LiveState", *, remaining_depth: int | None, model: str | None) -> Callable:
    """``compose_worker`` with ``remaining_depth`` compositions left to the agent holding it.

    ``None`` means "read the cap when called", which is what the tool given to a real agent is built
    with. A nested tool carries a concrete count instead, so lowering the cap mid-turn cannot extend a
    chain that is already running.

    The recursion lives here and only here. Since a composed worker refers to no declared agent, there
    is no graph for ``validate_agents`` to prove acyclic, so this decrement is the whole termination
    argument, and it is a closure variable precisely so nothing the model writes can influence it.
    """

    @tool
    async def compose_worker(name: str, task: str, tools: list[str], instructions: str) -> str:
        """Build a sub-agent with exactly the capabilities a task needs, run one task on it, and return
        its answer.

        Prefer spawn_subagent and one of its named roles. Use this only when no role fits: it costs an
        extra step, and a declared role carries instructions written for its job. Call
        list_capabilities first to see what the names are. The worker shares no history with you, so
        the task must be self-contained, and it is discarded afterwards.

        Args:
            name: A short kebab-case label for this worker, used in the logs and the sub-agent card,
                e.g. "quote-checker".
            task: The complete, self-contained task for the worker to carry out.
            tools: The capability names to give it, from list_capabilities, e.g. ["web", "compute"].
            instructions: The worker's standing instructions: its role, and how to report back.
        """
        label = name.strip() or DEFAULT_WORKER_NAME
        if not tools:
            return (
                f"No capabilities named for {label!r}. Call list_capabilities to see what is installed, "
                "then pass the names this task needs."
            )
        depth = _max_depth(state.config) if remaining_depth is None else remaining_depth
        if depth <= 0:
            return (
                "Composing a worker is switched off: [capabilities].max_depth is 0. Use spawn_subagent "
                "and one of your named roles, or ask the user to raise the setting."
            )
        # A call spends one, so the worker gets a tool of its own only while a composition would remain
        # after this one.
        nested = _make_compose_tool(state, remaining_depth=depth - 1, model=model) if depth > 1 else None
        try:
            spec = _compose_spec(label, tools, instructions, state, compose_tool=nested, model=model)
        except ToolsetError as error:
            return f"Could not compose {label!r}: {error}"
        spawn = make_async_subagent_tool(
            model,
            agent_types={label: spec},
            max_depth=1,
            tool_approval=state.tool_approval,
            observer=state.observer,
        )
        return await spawn(label, task)

    return compose_worker


def make_capability_tools(ctx: "ToolsetContext") -> list:
    state = ctx.state
    # The [assistant] default, falling back to whatever the holder's own client already resolved. That
    # is the fallback `make_delegation_tool` takes, for the same reason: with no default set, resolving
    # per composition would pick a model afresh each time instead of staying on the one in use.
    model = state.config.model or getattr(getattr(ctx.agent, "model_client", None), "model", None)

    @tool
    async def list_capabilities(filter: str = "") -> str:
        """List every capability installed on this machine, whether or not you currently hold it.

        Each line is a capability name, the kind of provider it came from, and what it does. Use this
        when a task needs something none of your own tools and none of your named sub-agent roles
        cover, then pass the names you need to compose_worker.

        Args:
            filter: Optional. Show only capabilities whose name or description contains this text.
        """
        return _catalogue(state.registry, filter)

    return [list_capabilities, _make_compose_tool(state, remaining_depth=None, model=model)]


TOOLSET = Toolset(
    name="capabilities",
    description="Discover every installed capability and compose a sub-agent from the ones a task needs.",
    build=make_capability_tools,
    settings=CAPABILITIES_SETTINGS,
    # Discovering and composing is how an agent manages its own work rather than a domain capability,
    # so a lean supervisor declaring only it still reads as lean to the delegation guidance.
    cross_cutting=True,
)
