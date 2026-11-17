"""The toolset registry: one namespace over every capability an agent can declare.

Names are global on purpose. An agent's ``tools`` list says ``"stocks"``, not ``"mcp:stocks"``, so a
capability's provenance is not part of the interface an agent depends on and a capability can move
between providers without touching any agent. The cost of that is paid here: two providers claiming one
name is a startup error rather than a silent shadowing, since the loser would vanish with no signal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Hashable, Iterable, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from kokua.config.schema import AssistantConfig
    from kokua.registry.context import ToolsetContext
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
    know where a core setting's value lives to own one of their own.
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

    ``parts`` lets one capability be granted at narrower scopes than the whole: it maps names the
    config derives (one document collection's ``work`` and ``work_write``, say) to the part each grants,
    and an agent may name those in its ``available`` list instead of the toolset. ``select`` resolves
    them back into this one toolset with ``build_parts`` bound to the parts named, so a narrow grant
    still builds one toolset under one name, and one approval vocabulary covers it however it was granted.
    """

    name: str
    description: str
    build: Callable[["ToolsetContext"], list]
    guidance: str = ""
    cross_cutting: bool = False
    entry_point_only: bool = False
    workflow: Optional["Workflow"] = None
    settings: tuple[Setting, ...] = ()
    parts: Optional[Callable[["AssistantConfig"], Mapping[str, Hashable]]] = None
    build_parts: Optional[Callable[["ToolsetContext", frozenset[Hashable]], list]] = None


class ToolsetRegistry(dict):
    """A ``dict[str, Toolset]`` that also remembers which source registered each name.

    Every ordinary consumer (``select``, ``build_tools``, ``LiveState.registry``) only ever needs the
    mapping, so this stays a plain dict for all of them. ``providers`` answers the questions that have to
    come from provenance rather than from anything a ``Toolset`` itself carries: which names are skills
    (``core.agents.without_skill_names``), what order ``available = "all"`` lists toolsets in
    (``core.agents.TRUST_ORDER``), and how ``--list-toolsets`` groups them.

    ``parts`` maps each part name a toolset declared (see ``Toolset.parts``) to ``(toolset name, part)``.
    It is empty until :func:`with_parts` fills it, because part names come from the config and
    registration does not read one.
    """

    def __init__(
        self,
        toolsets: Mapping[str, Toolset],
        providers: Mapping[str, str],
        parts: Mapping[str, tuple[str, Hashable]] | None = None,
    ):
        super().__init__(toolsets)
        self.providers: dict[str, str] = dict(providers)
        self.parts: dict[str, tuple[str, Hashable]] = dict(parts or {})


#: The approval-entry prefix (``confirm`` and ``review``) for the tools no toolset builds: ``spawn_subagent``, attached
#: to the entry agent after its toolsets are, and ``activate_skill``, which AIMU gives a ``SkillAgent``.
#: Reserved here rather than only in the gate's own parser, because a registered toolset of this name
#: would make those two gates unwritable, and a gate nobody can write is the failure this whole check
#: exists to prevent. See ``core.agents.gateable_tools``.
RESERVED_GATE_NAMESPACE = "core"

#: The string ``available = "all"`` is written with. A toolset of this name could never be declared on its
#: own: the bare string means every toolset, and the parser refuses it inside a list.
ALL_DECLARATION = "all"


def register(sources: Sequence[tuple[str, Iterable[Toolset]]]) -> ToolsetRegistry:
    """Index every toolset by name, rejecting a name two providers claim.

    Each source is ``(provider_label, toolsets)``; the label appears in the collision message, which is
    why sources are labeled rather than flattened by the caller. Two toolsets from the *same* provider
    (two MCP servers whose names both derive from one host, say) collide under one label, which alone
    would leave nothing to tell them apart, so the message also carries each side's ``description`` --
    for an MCP server that already names its URL, so both entries in the config are identifiable without
    a new field.

    Two names are refused outright rather than on collision: :data:`RESERVED_GATE_NAMESPACE` and
    :data:`ALL_DECLARATION`.
    """
    registry: dict[str, Toolset] = {}
    provider: dict[str, str] = {}
    for label, toolsets in sources:
        for toolset in toolsets:
            if toolset.name == RESERVED_GATE_NAMESPACE:
                raise ToolsetError(
                    f"toolset name {RESERVED_GATE_NAMESPACE!r} (from {label}) is reserved. It is the "
                    "approval-entry prefix (confirm and review) for the tools no toolset provides, so a toolset "
                    "holding it would leave those gates with no name to write. Rename it."
                )
            if toolset.name == ALL_DECLARATION:
                raise ToolsetError(
                    f'toolset name {ALL_DECLARATION!r} (from {label}) is reserved. available = "all" means '
                    "every installed toolset, so no agent could declare a toolset holding that name. Rename it."
                )
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


def with_parts(registry: ToolsetRegistry, config: "AssistantConfig") -> ToolsetRegistry:
    """``registry`` with every toolset's part names resolved against ``config``.

    A part name shares the namespace an agent's ``available`` list is written in, so it is refused on
    the same terms a toolset name is: equal to a toolset, to another part, or to a reserved name, it
    would make some declaration mean two things, and the loser would vanish with no signal.
    """
    parts: dict[str, tuple[str, Hashable]] = {}
    for toolset in registry.values():
        if (toolset.parts is None) != (toolset.build_parts is None):
            raise ToolsetError(
                f"toolset {toolset.name!r} declares only one of parts and build_parts. Declare both: parts "
                "names what an agent may be granted, and build_parts builds the tools for that grant."
            )
        if toolset.parts is None:
            continue
        for part_name, part in toolset.parts(config).items():
            if part_name in (RESERVED_GATE_NAMESPACE, ALL_DECLARATION):
                raise ToolsetError(f"toolset {toolset.name!r} declares part {part_name!r}, which is reserved.")
            if part_name in registry:
                raise ToolsetError(
                    f"toolset {toolset.name!r} declares part {part_name!r}, which is also the name of the "
                    f"toolset {part_name!r}. Rename the part: an agent names either without saying which it means."
                )
            if part_name in parts:
                raise ToolsetError(
                    f"part {part_name!r} is declared by both toolset {parts[part_name][0]!r} and toolset "
                    f"{toolset.name!r}. Rename one: an agent names a part without saying whose it is."
                )
            parts[part_name] = (toolset.name, part)
    return ToolsetRegistry(registry, registry.providers, parts)


def canonical_names(names: Sequence[str], registry: ToolsetRegistry) -> list[str]:
    """``names`` with each part name replaced by its toolset's name, deduplicated in order.

    For the callers that ask which toolsets an agent holds rather than at what scope. A name that is
    neither a part nor a toolset passes through unchanged, since validating names is ``select``'s job.
    The caller expands ``available = "all"`` first: the sentinel raises if iterated.
    """
    canonical: list[str] = []
    for name in names:
        toolset_name = registry.parts[name][0] if name in registry.parts else name
        if toolset_name not in canonical:
            canonical.append(toolset_name)
    return canonical


def entry_point_only_reason(entry_point: str) -> str:
    """Why an ``entry_point_only`` toolset is refused on any other agent, as the end of a sentence.

    Shared so a list declaration naming one and an ``available = "all"`` agent's ``resident`` naming one
    are refused in the same words.
    """
    return f"which only works on the entry point agent ({entry_point!r}). Spawned agents cannot host it."


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

    A part name (see ``Toolset.parts``) resolves to its toolset, at the position of that toolset's first
    mention. Naming the toolset whole anywhere in ``names`` grants it whole, since the whole already
    holds every part; otherwise every part named is collected into one copy whose ``build`` is
    ``build_parts`` bound to them.
    """
    # A plain dict is still a valid registry here: only one built by `with_parts` carries part names.
    parts: Mapping[str, tuple[str, Hashable]] = getattr(registry, "parts", {})
    order: list[str] = []
    granted: dict[str, set[Hashable] | None] = {}  # None is a whole grant
    for name in names:
        toolset_name, part = parts[name] if name in parts else (name, None)
        toolset = registry.get(toolset_name)
        if toolset is None:
            raise ToolsetError(_unknown_name(name, registry, parts, agent))
        if toolset.entry_point_only and agent != entry_point:
            raise ToolsetError(f"agent {agent!r} declares toolset {name!r}, {entry_point_only_reason(entry_point)}")
        if toolset_name not in granted:
            order.append(toolset_name)
            granted[toolset_name] = set()
        if name not in parts:
            granted[toolset_name] = None
        elif granted[toolset_name] is not None:
            granted[toolset_name].add(part)
    return [_bound(registry[name], granted[name]) for name in order]


def _unknown_name(
    name: str, registry: Mapping[str, Toolset], parts: Mapping[str, tuple[str, Hashable]], agent: str
) -> str:
    """Why ``agent`` cannot declare ``name``, which is neither a toolset nor a part.

    A name one ``_write`` away from a part is almost always the writable grant of a part its config left
    read-only, and the fix is in that config rather than in the list of names, so it gets its own sentence.
    """
    read_only = name.removesuffix("_write")
    if read_only != name and read_only in parts:
        owner = parts[read_only][0]
        return (
            f"agent {agent!r} declares {name!r}, but {name!r} is not a part of toolset {owner!r}: "
            f"{read_only!r} is, read-only. Declare it writable where {read_only!r} is configured, or "
            f"declare {read_only!r} instead."
        )
    available = ", ".join(sorted([*registry, *parts])) or "(none)"
    return f"agent {agent!r} declares unknown toolset {name!r}. Available toolsets: {available}."


def _bound(toolset: Toolset, parts: set[Hashable] | None) -> Toolset:
    """``toolset`` as granted: itself when whole, else a copy building only ``parts``."""
    if parts is None:
        return toolset
    build_parts, frozen = toolset.build_parts, frozenset(parts)
    return replace(toolset, build=lambda ctx: build_parts(ctx, frozen))


def build_tools(toolsets: Sequence[Toolset], ctx: "ToolsetContext") -> list:
    """Every selected toolset's tools, concatenated, deduplicated by ``__name__`` keeping the first.

    First-wins matches the declared order, so an agent that wants one toolset's version of a shared
    tool name declares that toolset earlier.

    Each name is also recorded on ``ctx.state`` **under the toolset that offered it**, which is what
    lets startup resolve a ``confirm`` or ``review`` entry written as ``toolset.tool`` and reject one
    that names no real tool. This is the only place that recording can happen once: every agent's
    registry-provided tools are built through this function, the entry agent's and a spawned worker's
    alike, and a nested worker's are built by a recursion that lands here too. Collecting at any of the
    callers instead would need one collector per call site and would miss whichever depth was forgotten.

    Note the recording is deliberately wider than the returned list. A name the first-wins dedup drops
    is still recorded under the toolset that offered it, because that toolset really does provide it and
    would win for an agent declaring it first. Recording only the survivors would make a truthful gate
    entry fail startup on the accident of one agent's declaration order.
    """
    tools: list = []
    seen: set[str] = set()
    for toolset in toolsets:
        # What this call built, kept apart from the shared record below: a concurrent build of the same
        # toolset in another worker thread may be adding to that set while the hook reads.
        built: set[str] = set()
        # Seeded even when the build returns nothing, so a workflow-only toolset like `planning` is
        # known-and-empty to the gate check rather than indistinguishable from a name nobody declared.
        provided = ctx.state.tools_by_toolset.setdefault(toolset.name, set())
        for fn in toolset.build(ctx):
            name = getattr(fn, "__name__", None)
            if not name:
                continue
            provided.add(name)
            built.add(name)
            if name not in seen:
                seen.add(name)
                tools.append(fn)
        # After recording and before the tools go back to the caller: approval has to cover a toolset
        # however late, and by whomever, it is built, and a raising hook refuses the whole build.
        if ctx.state.toolset_built is not None:
            ctx.state.toolset_built(toolset.name, frozenset(built))
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
