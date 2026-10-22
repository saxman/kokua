"""The approval axis: which tool calls ask before they run, and who answers.

Every agent has an approval policy with two disjoint halves. A ``confirm`` tool asks the user. A
``review`` tool asks a reviewer first, and falls back to the user when the reviewer declines or cannot
answer. An agent declares its pair in ``[agents.<name>.tools]``; an agent that declares none inherits
``[assistant.tools]`` whole, never per key, so a tool cannot end up in both halves by merging.

Policies resolve after every agent is wired, not at config parse, because a gate entry is written in
the ``<toolset>.<tool>`` vocabulary and the tool names behind it exist only once the toolsets have
built them. That is also why the gate itself is bound by tool name and looked up at call time: the
approval hook sees a name and the agent making the call, and asks the resolved policy what to do.

Resolution is a startup check as much as a lookup. An entry matching no tool, naming a toolset the
agent cannot reach, or landing a tool in both halves fails startup, because a gate that holds nothing
back is the one mistake whose symptom is the absence of a symptom.

Startup is not the last time an entry resolves. Every build of a toolset, whoever triggers it, reports
the tools it built (``LiveState.toolset_built``), and every entry naming that toolset is matched again
against them, the resolved ones as well as the ones still waiting. A toolset's tools can grow after
startup: a skill gains a script mid-session, and ``review = ["weekly-digest"]`` means every script that
skill has, not the ones it had when Kokua started. So each policy keeps the entries it was written with
beside the names they resolved to, and a build only ever adds names, before any of its tools is returned.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kokua.config import ConfigError
from kokua.config.schema import DEFAULT_POLICY_SOURCE
from kokua.core.agents import (
    available_for,
    built_toolsets,
    certain_overlap,
    core_tool_names_for_agent,
    deferred_for,
    gateable_tools,
    pending_gate_entries,
    resolve_built_entries,
    resolve_gate_entries,
)
from kokua.registry.registry import RESERVED_GATE_NAMESPACE

if TYPE_CHECKING:
    from kokua.config.schema import AssistantConfig
    from kokua.registry.context import LiveState

#: The default tier's name (defined with the config, which labels entries by it). No agent can have it.
DEFAULT_POLICY = DEFAULT_POLICY_SOURCE

_GATE_EFFECT = "gate nothing"
_GATE_REMEDY = (
    "An entry matching no tool holds nothing back, so the call it was written to stop runs with no "
    "prompt and nothing reports it. Only tools that exist at startup can be gated, so a tool from a "
    "server the assistant connects later with add_mcp_server cannot be listed ahead of time: give the "
    "server a [[mcp.server]] table in config.toml and name it in an agent's tools, and its tools are "
    "gateable from the next start."
)


@dataclass(frozen=True)
class ApprovalPolicy:
    """One agent's resolved policy, as tool names. ``source`` is the table the entries came from."""

    agent: str
    source: str
    confirm: frozenset[str]
    review: frozenset[str]
    # Raw entries whose toolset is deferred and not yet built by anyone. They hold nothing back until
    # `ApprovalPolicies.with_built` resolves them, which happens before the build returns any tool, so a
    # tool is never callable while its entry is still waiting.
    pending_confirm: frozenset[str] = frozenset()
    pending_review: frozenset[str] = frozenset()
    # Every entry as written, resolved or pending, so a later build of a toolset can match them again.
    confirm_entries: frozenset[str] = frozenset()
    review_entries: frozenset[str] = frozenset()

    @property
    def gated(self) -> frozenset[str]:
        return self.confirm | self.review


@dataclass(frozen=True)
class ApprovalPolicies:
    """The default policy plus the pair each declaring agent carries."""

    by_agent: Mapping[str, ApprovalPolicy]
    default: ApprovalPolicy

    def for_agent(self, name: str) -> ApprovalPolicy:
        """The declared policy for ``name``, else the default relabelled with its name.

        A name nobody declared (a worker composed for one task) takes the default tier, so the lookup
        never fails and never leaves a call ungoverned.
        """
        declared = self.by_agent.get(name)
        if declared is not None:
            return declared
        return dataclasses.replace(self.default, agent=name)

    def with_built(self, toolset: str, tool_names: frozenset[str]) -> ApprovalPolicies:
        """These policies with every entry naming ``toolset`` matched against what it built, added to the
        names each already holds.

        Approval has to hold for a toolset however late, and by whomever, it is built: an agent loading
        it, a worker composed for one task, or a skill refresh after a new script. So the build itself
        calls this (through ``LiveState.toolset_built``) before it returns a single tool, and a pending
        entry that matches nothing raises there, refusing the build, because a gate naming a tool that
        does not exist holds nothing back. Pure: returns a new object, so a reader holding the old one
        never sees a half-updated pair.
        """
        policies = {name: _with_built(policy, toolset, tool_names) for name, policy in self.by_agent.items()}
        return ApprovalPolicies(by_agent=policies, default=_with_built(self.default, toolset, tool_names))


def _with_built(policy: ApprovalPolicy, toolset: str, tool_names: frozenset[str]) -> ApprovalPolicy:
    confirm, pending_confirm = resolve_built_entries(
        policy.confirm_entries, policy.pending_confirm, toolset, tool_names, setting=f"{policy.source}.confirm"
    )
    review, pending_review = resolve_built_entries(
        policy.review_entries, policy.pending_review, toolset, tool_names, setting=f"{policy.source}.review"
    )
    confirm |= policy.confirm
    review |= policy.review
    _refuse_overlap(policy.source, confirm & review)
    return dataclasses.replace(
        policy,
        confirm=frozenset(confirm),
        review=frozenset(review),
        pending_confirm=frozenset(pending_confirm),
        pending_review=frozenset(pending_review),
    )


def _refuse_overlap(source: str, both: Iterable[str]) -> None:
    """Refuse a pair whose halves share ``both``: tool names once resolved, or entries still pending."""
    both = set(both)
    if both:
        raise ConfigError(
            f"{source} gates {', '.join(sorted(both))} in both confirm and review. "
            "A gated tool either asks you (confirm) or asks a reviewer first (review); pick one."
        )


def _vocabulary_for(
    name: str, config: AssistantConfig, state: LiveState, full: dict[str, set[str]]
) -> dict[str, set[str]]:
    """The toolsets ``name`` can reach, with their tools, plus the reserved ``core`` namespace.

    The entry agent also reaches every skill's toolset, because its ``SkillAgent`` holds every skill
    whether or not it declares one.
    """
    reachable = set(available_for(config, state.registry, name)) | {RESERVED_GATE_NAMESPACE}
    if name == config.entry_agent:
        reachable |= {skill.name for skill in state.skill_manager.skills.values()}
    core_names = core_tool_names_for_agent(config, state.registry, name)
    return {
        toolset: tools & core_names if toolset == RESERVED_GATE_NAMESPACE else tools - core_names
        for toolset, tools in full.items()
        if toolset in reachable
    }


def _resolve_pair(
    agent: str,
    source: str,
    confirm: list[str],
    review: list[str],
    vocabulary: dict[str, set[str]],
    full: dict[str, set[str]],
    state: LiveState,
    deferred: set[str],
) -> ApprovalPolicy:
    """Resolve one pair; entries naming a deferred, unbuilt toolset wait in ``pending_*`` instead.

    ``deferred`` is the toolsets this pair's holder(s) can load later. One already built, in the sense of
    :func:`~kokua.core.agents.built_toolsets` (a recorded build, or a skill the entry agent's
    ``SkillAgent`` holds), resolves now like any other, so only a toolset that is not built yet waits.
    """
    resolved = {}
    pending = {}
    built = built_toolsets(state)
    for key, entries in (("confirm", confirm), ("review", review)):
        setting = f"{source}.{key}"
        waiting = pending_gate_entries(entries, deferred, built)
        pending[key] = frozenset(waiting)
        entries = [entry for entry in entries if entry not in waiting]
        for entry in entries:
            toolset = entry.split(".")[0]
            # A toolset that exists but is out of this agent's reach gets its own sentence. One that
            # exists nowhere falls through to the resolver, whose "did you mean" is the better answer.
            if toolset in full and toolset not in vocabulary:
                raise ConfigError(
                    f"{setting} names {entry!r}, but {agent} cannot reach the {toolset!r} toolset: "
                    f"it is not in [agents.{agent}.tools].available."
                )
        resolved[key] = resolve_gate_entries(
            entries, vocabulary, state.registry, setting=setting, effect=_GATE_EFFECT, remedy=_GATE_REMEDY
        )
    _refuse_overlap(source, resolved["confirm"] & resolved["review"])
    # Pending entries are checked too, by their text: a conflict certain to appear at the build would
    # otherwise pass startup and then refuse every load of the toolset. `_with_built` still checks the
    # built names, for an overlap no entry's text reveals.
    _refuse_overlap(source, certain_overlap(pending["confirm"], pending["review"]))
    return ApprovalPolicy(
        agent=agent,
        source=source,
        confirm=resolved["confirm"],
        review=resolved["review"],
        pending_confirm=pending["confirm"],
        pending_review=pending["review"],
        confirm_entries=frozenset(confirm),
        review_entries=frozenset(review),
    )


def resolve_approval_policies(config: AssistantConfig, state: LiveState, entry_agent) -> ApprovalPolicies:
    """Resolve the default tier and every declared pair to tool names, rejecting any that gate nothing.

    The default tier resolves against every tool any agent built, since an inheriting agent may be any
    of them. A declared pair resolves against that agent's own toolsets, so it cannot gate a capability
    the agent does not hold.
    """
    full = gateable_tools(state, entry_agent)
    any_deferred = {toolset for name in config.agents for toolset in deferred_for(config, state.registry, name)}
    default = _resolve_pair(
        DEFAULT_POLICY, DEFAULT_POLICY, config.default_confirm, config.default_review, full, full, state, any_deferred
    )
    by_agent = {}
    for name, agent in config.agents.items():
        if agent.confirm is None or agent.review is None:
            continue
        confirm, review, source = config.approval_entries_for(name)
        vocabulary = _vocabulary_for(name, config, state, full)
        by_agent[name] = _resolve_pair(
            name, source, confirm, review, vocabulary, full, state, set(deferred_for(config, state.registry, name))
        )
    return ApprovalPolicies(by_agent=by_agent, default=default)
