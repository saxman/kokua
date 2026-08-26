"""What a turn cost: model calls, tokens, and seconds, accumulated from AIMU's run events.

A ``TurnMetrics`` is an AIMU event sink, which is one callable taking one event. It reads
``ModelTurnFinished`` and ignores every other member of the union. Ignoring is the correct default
rather than a gap: AIMU's events are a dataclass union precisely so that a new member cannot break
an existing consumer, and a sink that raised on an unknown event would make an AIMU upgrade a
Kokua outage.

Tokens are recorded absent rather than zero when no provider reported them. A local
OpenAI-compatible server may report nothing at all, and a stored zero becomes a rendered zero
becomes a false claim about what the run cost. ``reported_calls`` is what lets a reader tell a
turn that used no tokens from a turn nobody measured.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from aimu.events import ModelTurnFinished

# The running turn's accumulator, set by TurnRunner for the turn's duration and None outside one.
# A ContextVar rather than a field on anything shared, for the reason ``subagent_events`` is one
# (see ``core/subagents.py``): turns on different conversations run concurrently, and a shared
# field would let one conversation's model calls land in another conversation's record. A
# contextvar copy into a TaskGroup child copies the reference, so a turn's concurrent spawns
# accumulate into the one object that turn later persists.
current_metrics: ContextVar[Optional["TurnMetrics"]] = ContextVar("current_metrics", default=None)


def record_event(event) -> None:
    """Hand one event to the running turn's accumulator, if a turn is running.

    Every sink seam is wired with *this*, once, where the client or agent is built, rather than with
    a turn's own ``TurnMetrics``. That is what removes any attach-and-detach around a turn: the
    ContextVar decides whether an event is recorded, so a seam wired at build time is inert until a
    turn opens one. Being called outside a turn is the normal case, not an error.
    """
    metrics = current_metrics.get()
    if metrics is not None:
        metrics(event)


class TurnMetrics:
    """One turn's model calls, accumulated as they finish.

    Attribution is by the events' ``agent`` field, which mirrors ``StreamChunk.agent`` for exactly
    this purpose, so an entry agent's calls, a workflow critic's, and a spawned worker's stay
    distinguishable in a single sink. ``None`` is the entry agent, which is the only agent that
    names itself by not having a name.
    """

    # A fixed label, not read from config: the entry agent's actual name is whatever `[assistant].agent`
    # names it, which this module has no way to look up (and should not import `kokua.config` to do).
    # It exists at all only because `turns.py` wires `agent.model_client.events`, not `agent.events`, for
    # the entry agent's client; setting `agent.events` instead would give AIMU's own generated
    # `agent-<hex>` name to every entry-agent call, and this mapping from `None` would never fire.
    ENTRY_AGENT = "assistant"

    def __init__(self) -> None:
        self._calls: list[dict] = []

    def __call__(self, event) -> None:
        if not isinstance(event, ModelTurnFinished):
            return
        usage = event.usage if isinstance(event.usage, dict) else None
        self._calls.append(
            {
                "agent": event.agent or self.ENTRY_AGENT,
                "model": str(event.model or ""),
                "seconds": float(event.duration_s or 0.0),
                "input_tokens": _int_or_none(usage, "input_tokens"),
                "output_tokens": _int_or_none(usage, "output_tokens"),
            }
        )

    def record(self, wall_seconds: float) -> Optional[dict]:
        """This turn's totals, or None when it made no model call.

        None rather than an empty dict so the caller skips the write entirely: a stored record of
        no calls renders as a turn that somehow cost nothing, which is a claim, where a missing
        one renders as nothing at all, which is the truth.
        """
        if not self._calls:
            return None
        record = _totals(self._calls)
        record["wall_seconds"] = round(float(wall_seconds), 1)
        # Kept even though no renderer reads it yet: it is the honest answer to which models a
        # delegating turn actually used, where `metadata["model"]` names only the one that answered.
        record["models"] = list(dict.fromkeys(call["model"] for call in self._calls if call["model"]))
        agents = {call["agent"] for call in self._calls}
        if len(agents) > 1:
            record["by_agent"] = {
                agent: _totals([call for call in self._calls if call["agent"] == agent]) for agent in sorted(agents)
            }
        return record


def _int_or_none(usage: Optional[dict], key: str) -> Optional[int]:
    """One usage figure as an int, or None when the provider did not report it.

    Guarded rather than trusted because ``last_usage`` is provider-shaped: a server that omits the
    key, or answers with null, is reporting no figure and must not be read as reporting zero.
    """
    if not usage:
        return None
    value = usage.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _totals(calls: list[dict]) -> dict:
    """Sum one set of calls, omitting a token key no call in it reported."""
    totals: dict = {
        "calls": len(calls),
        "model_seconds": round(sum(call["seconds"] for call in calls), 1),
    }
    # A call counts as "reported" if either token key came back, not only when both did, so
    # `reported_calls` is not itself split by key; a call that reported only one of the two is not
    # distinguishable from one that reported both once it lands in this count.
    reported = [call for call in calls if call["input_tokens"] is not None or call["output_tokens"] is not None]
    if reported:
        totals["reported_calls"] = len(reported)
        for key in ("input_tokens", "output_tokens"):
            figures = [call[key] for call in reported if call[key] is not None]
            if figures:
                totals[key] = sum(figures)
    return totals
