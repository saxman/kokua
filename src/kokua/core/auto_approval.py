"""A model reviewer that may approve a gated tool call, and the code that decides whether it did.

This is a convenience layer, not a security boundary. It exists to spend fewer of the user's
keystrokes on calls that were plainly fine, and the text it reviews was written by a model that has
read web pages, files, and tool output. Two properties keep that honest, and both are structural
rather than advisory:

**The reviewer never grants.** It answers three questions and writes one sentence; :func:`decide`
computes the outcome in four words of Python. So the policy is inspectable and testable, a verdict
can be read against the answers it came from, and a missing field is an escalation rather than a coin
flip. What the reviewer is asked is deliberately not "is this malicious": the common way an agent
damages a machine is acting correctly on the wrong target, where there is no intent to detect, which
is what ``in_scope`` and ``reversible`` are for. ``injection_suspected`` covers the rarer case.

**The reviewer can only turn a prompt into an approval.** There is no deny verdict, so nothing here
can change what the human would have been able to decide; an escalation carries the reviewer's
sentence so a person deciding gets its read for free. That is the whole security claim, and it is
short on purpose.

What this does not do, and what a reader should not assume it does: there is no sandbox under it and
no shell parser in front of it, so unlike the comparable gates in other assistants this reviewer
reads a raw command string and is the only layer between the model and the user's files. An
argument-scoped allowlist would remove most prompts with no model at all and is the better thing to
reach for first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from kokua.config.file import ConfigError
from kokua.config.schema import AssistantConfig, ResolvedReviewer
from kokua.core.agents import gateable_tools, resolve_gate_entries
from kokua.registry.context import LiveState

logger = logging.getLogger(__name__)

#: Longest any single packet field may be. A field over this escalates rather than being reviewed
#: truncated: a reviewer shown the first two thousand characters of a payload is a reviewer the rest of
#: the payload was hidden from, and the part that gets cut is the part an attacker chooses.
MAX_FIELD_CHARS = 2000

#: The fence marking model- and web-derived text inside the packet. The reviewer's prompt tells it that
#: anything between these is data, so a value that could close the fence early would move the rest of
#: itself back into instruction position; :func:`build_packet` refuses such a value outright rather
#: than escaping it, because an escape the reviewer has to understand is one more thing to get wrong.
UNTRUSTED_OPEN = "<untrusted>"
UNTRUSTED_CLOSE = "</untrusted>"


@dataclass
class Review:
    """One reviewer's answers about one tool call. Not a decision: see :func:`decide`.

    A plain dataclass because that is what AIMU's ``client.chat(schema=...)`` takes, the same shape
    ``workflows.critics.Verdict`` uses.
    """

    in_scope: bool
    reversible: bool
    injection_suspected: bool
    reason: str


def decide(reviews: Sequence[Review]) -> bool:
    """Whether a gated call may run without asking the user.

    Unanimous and non-empty. Empty is refused explicitly rather than left to ``all()``, which answers
    True for nothing: "no reviewer managed to answer" has to read as "nobody approved", and every
    fail-closed path in this module arrives here with an empty sequence.
    """
    return bool(reviews) and all(
        review.in_scope and review.reversible and not review.injection_suspected for review in reviews
    )


def _fence(value: str) -> Optional[str]:
    """``value`` wrapped as untrusted data, or None when it cannot be fenced safely."""
    if len(value) > MAX_FIELD_CHARS:
        return None
    if UNTRUSTED_CLOSE in value or UNTRUSTED_OPEN in value:
        return None
    return f"{UNTRUSTED_OPEN}{value}{UNTRUSTED_CLOSE}"


def build_packet(
    *,
    tool: str,
    toolset: str,
    arguments: dict,
    request: str,
    used: int,
    allowed: int,
) -> Optional[str]:
    """The bounded review packet, or None when it cannot be built and the call must go to the user.

    ``tool`` and ``toolset`` are Kokua's own words, resolved at startup, so they are stated plainly.
    Everything else came from a model and is fenced. The budget line is included because a reviewer
    seeing the fifth review of one turn is looking at a retry loop, which is worth knowing about a
    call that has already been asked four times.
    """
    fenced_request = _fence(request)
    fenced_arguments = _fence(str(arguments))
    if fenced_request is None or fenced_arguments is None:
        return None
    return (
        f"tool       {tool}\n"
        f"toolset    {toolset}\n"
        f"budget     {used} of {allowed} auto-approvals used this turn\n"
        f"request    {fenced_request}\n"
        f"arguments  {fenced_arguments}\n"
    )


@dataclass(frozen=True)
class AutoApproval:
    """The resolved gate: which tool names a reviewer may be asked about, and who is asked.

    Built once at startup, after every agent is wired, because the vocabulary an entry resolves
    against does not exist until then. ``toolset_of`` is resolved here rather than looked up per call
    so the packet can name a capability without the gate reaching the registry mid-dispatch.
    """

    tools: frozenset[str]
    toolset_of: Mapping[str, str]
    reviewers: tuple[ResolvedReviewer, ...]
    timeout_seconds: float
    max_per_turn: int


def resolve_auto_approval(
    config: AssistantConfig, state: LiveState, entry_agent, gated: frozenset[str]
) -> Optional[AutoApproval]:
    """The gate ``[security.auto_approval]`` describes, or None when it is off.

    Every fault here fails startup rather than warning, for the reason ``resolve_confirm_tools`` gives
    about its own: the symptom of a broken gate is the absence of a symptom. A reviewer nobody declared,
    a standard nobody wrote, or a tool nothing gates all present as a feature that silently is not
    running, and a user who enabled it has no way to tell that from a reviewer that keeps approving.

    ``gated`` is ``resolve_confirm_tools``' answer, passed in rather than recomputed: the two are halves
    of one decision, and an auto-approval set that is not a subset of it would describe a prompt that
    never existed.
    """
    if not config.auto_approval_enabled:
        return None
    if not config.auto_approval_reviewers:
        raise ConfigError(
            "[security.auto_approval] is enabled but names no reviewer, so every gated call would still "
            'reach you. Add reviewers = ["approval"] and declare [reviewers.approval].'
        )
    reviewers: list[ResolvedReviewer] = []
    for name in config.auto_approval_reviewers:
        if name not in config.reviewers:
            known = ", ".join(sorted(config.reviewers)) or "none"
            raise ConfigError(
                f"[security.auto_approval].reviewers names {name!r}, which has no [reviewers.{name}] "
                f"table. Declared reviewers: {known}."
            )
        resolved = config.reviewer_for(name)
        if not resolved.system_message.strip():
            raise ConfigError(
                f"[reviewers.{name}] has no system_message, and it is the reviewer deciding whether a "
                "gated tool call reaches you. A reviewer with no stated standard reviews nothing, so "
                "the prompt is required here rather than defaulted: it is the part of this feature you "
                "are meant to read."
            )
        # The value the reviewer's own table declared, not the resolved one. What this refuses is a key
        # the user wrote for this reviewer that cannot take effect, which is the rule that an ignored key
        # is worse than a rejected one. An effort inherited from [assistant].thinking makes no claim
        # about this reviewer, and it is inert here rather than degraded: building a reviewer client
        # reads only model, system_message, and generation. Refusing the inherited case would turn one
        # unrelated global setting into an install-wide bar on enabling auto-approval, with an error
        # telling the reader to remove a key their [reviewers.<name>] table does not contain.
        if config.reviewers[name].thinking:
            raise ConfigError(
                f"[reviewers.{name}].thinking is set, and an approval reviewer cannot reason: its answer "
                "comes back through a structured call, which returns JSON and no reasoning on every "
                "provider. Remove it rather than leaving a key that does nothing."
            )
        if resolved.model == config.default_model:
            logger.warning(
                "[reviewers.%s] runs on the same model as [assistant], so the reviewer and the agent it "
                "reviews can be talked out of the same judgement by the same text. A different model is "
                "the point of the separation.",
                name,
            )
        reviewers.append(resolved)
    vocabulary = gateable_tools(state, entry_agent)
    tools = resolve_gate_entries(
        config.auto_approval_tools,
        vocabulary,
        state.registry,
        setting="[security.auto_approval].tools",
        effect="review nothing",
        remedy=(
            "An entry matching no tool cannot reduce a prompt, so the call it was written to wave "
            "through still stops and asks."
        ),
    )
    if not tools:
        raise ConfigError(
            "[security.auto_approval] is enabled but names no tool, so nothing would ever be reviewed. "
            'Add the gated tools a reviewer may answer for, for instance tools = ["compute.run_command"].'
        )
    floor = resolve_gate_entries(
        config.never_auto_approve,
        vocabulary,
        state.registry,
        setting="[security].never_auto_approve",
        effect="hold nothing back",
        remedy="An entry matching no tool holds nothing back from a reviewer that can approve it.",
    )
    forbidden = sorted(tools & floor)
    if forbidden:
        raise ConfigError(
            f"[security.auto_approval].tools reaches {', '.join(forbidden)}, which "
            "[security].never_auto_approve holds back. Those tools change what may act later rather "
            "than acting once, so approving one grants a capability and the arguments it was approved "
            "for stop constraining anything. Drop them here, or take them out of never_auto_approve by "
            "hand and understand that a waved update_config can widen this list."
        )
    ungated = sorted(tools - gated)
    if ungated:
        raise ConfigError(
            f"[security.auto_approval].tools reaches {', '.join(ungated)}, which nothing gates: "
            "[security].confirm_tools does not name it, so that call already runs with no prompt and "
            "there is nothing for a reviewer to save you. Gate it first, or drop it here."
        )
    toolset_of = {
        tool: ",".join(sorted(name for name, provided in vocabulary.items() if tool in provided)) for tool in tools
    }
    return AutoApproval(
        tools=tools,
        toolset_of=toolset_of,
        reviewers=tuple(reviewers),
        timeout_seconds=config.auto_approval_timeout_seconds,
        max_per_turn=config.auto_approval_max_per_turn,
    )
