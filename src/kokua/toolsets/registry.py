"""The toolset registry: one namespace over every capability an agent can declare.

Names are global on purpose. An agent's ``tools`` list says ``"stocks"``, not ``"mcp:stocks"``, so a
capability's provenance is not part of the interface an agent depends on and a capability can move
between providers without touching any agent. The cost of that is paid here: two providers claiming one
name is a startup error rather than a silent shadowing, since the loser would vanish with no signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from kokua.toolsets.context import ToolsetContext
    from kokua.workflows import Workflow


class ToolsetError(RuntimeError):
    """A toolset could not be registered or resolved. Carries an actionable message: which name, which
    agent declared it, and what the valid choices are."""


@dataclass(frozen=True)
class Setting:
    """One ``config.toml`` key a toolset owns.

    The section is not named here: it is always the toolset's own name, which is what makes two
    toolsets unable to claim one section. The toolset namespace already rejects a duplicate name, so it
    rejects a duplicate section for free.

    ``hot`` means the setting can change without a restart, reaching the running session through the
    ``update_config``. Being flagged here *is* what makes it hot; there is no second
    list to keep in step.

    This is the author's declaration, deliberately separate from ``config.table.RuntimeSetting``, which
    is the table entry the applier and the persist path drive off. A toolset author should not have to
    know about ``AssistantConfig`` attribute names or channel mirroring to own a setting.
    """

    key: str
    kind: type
    default: Any
    hot: bool = False


@dataclass(frozen=True)
class Toolset:
    """One named capability an agent can declare in its ``tools`` list.

    ``build`` returns the tool callables for one agent and is called once per agent. It must create
    only closures, never process state: shared state (a memory store, the live MCP connections, the
    conversation book) belongs to ``LiveState`` and reaches ``build`` through the context. Two agents
    declaring the same toolset therefore share one store rather than opening two.

    ``guidance`` is appended to the system message of any agent holding this toolset, so a capability
    ships the instructions that make the model actually use it instead of relying on a prompt constant
    that has to be kept in step by hand.

    ``cross_cutting`` marks a toolset an agent holds to manage itself (memory, config, the clock)
    rather than to do domain work. It exists only so the delegation guidance can tell a lean agent from
    a tool-heavy one. It defaults to False so that a third-party toolset counts as domain work, which is
    the right assumption for one.

    ``entry_point_only`` marks a toolset that only functions on the agent Kokua constructs directly.
    Its one member is ``skills``: spawned workers are plain AIMU ``Agent`` instances, not
    ``SkillAgent``, so skill injection has nothing to hook.

    ``workflow`` is a custom turn strategy this capability offers (see :mod:`kokua.workflows`). An
    agent declaring the toolset gets the workflow's command; nothing else grants one, so a turn
    strategy is declared exactly the way a tool is. A workflow-bearing toolset may still return tools
    from ``build``, and that is how the same workflow reaches the model as a tool: return
    ``runner.as_tool(...)``.

    ``settings`` are the ``config.toml`` keys this toolset owns, in its own ``[<name>]`` section, so a
    capability ships its configuration alongside the code that reads it. Their values land in
    ``config.toolset_settings[<name>]``: a workflow reads them as attributes through
    ``WorkflowContext.settings``, and ``build`` reads them from ``ctx.config.toolset_settings[<name>]``,
    which is complete by then because every declared default is seeded at startup.
    """

    name: str
    description: str
    build: Callable[["ToolsetContext"], list]
    guidance: str = ""
    cross_cutting: bool = False
    entry_point_only: bool = False
    workflow: Optional["Workflow"] = None
    settings: tuple[Setting, ...] = ()


class ToolsetRegistry(dict):
    """A ``dict[str, Toolset]`` that also remembers which source registered each name.

    Every ordinary consumer (``select``, ``build_tools``, ``LiveState.registry``) only ever needs the
    mapping, so this stays a plain dict for all of them. ``providers`` exists solely for the startup
    warning that has to tell a name nobody provisioned (an AIMU built-in group, a core subsystem) from
    a name the user provisioned and then never referenced (a plugin, a configured MCP server); that
    distinction has to come from provenance, not from anything a ``Toolset`` itself carries.
    """

    def __init__(self, toolsets: Mapping[str, Toolset], providers: Mapping[str, str]):
        super().__init__(toolsets)
        self.providers: dict[str, str] = dict(providers)


def register(sources: Sequence[tuple[str, Iterable[Toolset]]]) -> ToolsetRegistry:
    """Index every toolset by name, rejecting a name two providers claim.

    Each source is ``(provider_label, toolsets)``; the label appears in the collision message, which is
    why sources are labeled rather than flattened by the caller. Two toolsets from the *same* provider
    (two MCP servers whose names both derive from one host, say) collide under one label, which alone
    would leave nothing to tell them apart, so the message also carries each side's ``description`` --
    for an MCP server that already names its URL, so both entries in the config are identifiable without
    a new field.
    """
    registry: dict[str, Toolset] = {}
    provider: dict[str, str] = {}
    for label, toolsets in sources:
        for toolset in toolsets:
            if toolset.name in registry:
                existing = registry[toolset.name]
                # Each side's description may already end in its own period (every MCP toolset's does:
                # "Tools from the MCP server at {url}."), so interpolating it straight into this sentence
                # and then ending the sentence too would render "...mcp.).". Stripped here rather than
                # asking every Toolset author to leave theirs off.
                existing_desc = existing.description.rstrip(".")
                new_desc = toolset.description.rstrip(".")
                raise ToolsetError(
                    f"toolset name {toolset.name!r} is claimed by two providers: "
                    f"{provider[toolset.name]} ({existing_desc}) and {label} ({new_desc}). "
                    "Rename one, or drop it: an agent names a toolset without saying what provides it, so "
                    "the name has to be unique."
                )
            registry[toolset.name] = toolset
            provider[toolset.name] = label
    return ToolsetRegistry(registry, provider)


def select(
    names: Sequence[str],
    registry: Mapping[str, Toolset],
    *,
    agent: str,
    entry_point: str,
) -> list[Toolset]:
    """The toolsets ``names`` refers to, in declared order, deduplicated.

    An unresolvable name raises rather than dropping. Dropping is what the previous per-role
    ``groups``/``tool_packs`` lists did, and a typo silently produced a smaller toolset with nothing to
    say so.
    """
    selected: list[Toolset] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        toolset = registry.get(name)
        if toolset is None:
            available = ", ".join(sorted(registry)) or "(none)"
            raise ToolsetError(f"agent {agent!r} declares unknown toolset {name!r}. Available toolsets: {available}.")
        if toolset.entry_point_only and agent != entry_point:
            raise ToolsetError(
                f"agent {agent!r} declares toolset {name!r}, which only works on the entry point agent "
                f"({entry_point!r}). Spawned agents cannot host it."
            )
        seen.add(name)
        selected.append(toolset)
    return selected


def build_tools(toolsets: Sequence[Toolset], ctx: "ToolsetContext") -> list:
    """Every selected toolset's tools, concatenated, deduplicated by ``__name__`` keeping the first.

    First-wins matches the declared order, so an agent that wants one toolset's version of a shared
    tool name declares that toolset earlier.

    Each name is also recorded on ``ctx.state``, which is what lets startup reject a
    ``[security].confirm_tools`` entry that names no real tool. This is the only place that recording
    can happen once: every agent's registry-provided tools are built through this function, the entry
    agent's and a spawned worker's alike, and a nested worker's are built by a recursion that lands here
    too. Collecting at any of the callers instead would need one collector per call site and would miss
    whichever depth was forgotten.
    """
    tools: list = []
    seen: set[str] = set()
    for toolset in toolsets:
        for fn in toolset.build(ctx):
            name = getattr(fn, "__name__", None)
            if name and name not in seen:
                seen.add(name)
                tools.append(fn)
    ctx.state.built_tool_names.update(seen)
    return tools


def workflows_of(toolsets: Sequence[Toolset]) -> list[tuple[str, "Workflow"]]:
    """Each workflow ``toolsets`` offers, paired with the name of the toolset carrying it, in declared order.

    Paired rather than bare, because a caller that has to report a problem with a workflow needs to name
    the toolset the user would edit, and because a workflow's config section is its carrier's.

    Separate from ``select`` because availability and resolution answer different questions: ``select``
    resolves what one agent holds, while a command map is a property of the entry agent alone (a spawned
    worker has no channel to invoke a command on).
    """
    return [(toolset.name, toolset.workflow) for toolset in toolsets if toolset.workflow is not None]
