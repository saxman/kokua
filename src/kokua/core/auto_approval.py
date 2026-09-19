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

from dataclasses import dataclass
from typing import Optional, Sequence

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
