"""A reviewer builds its own client outside any caller's scoped sink, so its cost has to be wired
in rather than threaded through as a parameter. See :func:`kokua.workflows.critics.reviewer_agent`."""

from aimu.events import ModelTurnFinished

from kokua.core.metrics import TurnMetrics, current_metrics, record_event
from kokua.workflows.critics import reviewer_agent


def test_a_reviewer_reports_its_model_turns_to_the_metrics_forwarder():
    """A critic builds its own client, so without an explicit sink its cost is invisible.

    ``reviewer_agent`` takes no ``events`` parameter: the module-level forwarder is wired
    unconditionally, since it reads the running turn off a contextvar at emit time and needs no
    caller to pass it through.
    """
    agent = reviewer_agent(None, "review it")
    assert agent.events is record_event


def test_a_critics_model_turn_lands_in_the_running_turns_record():
    """The forwarder plus the contextvar, end to end: an event raised from inside a review reaches
    the accumulator the turn opened, attributed to the critic rather than folded into the entry
    agent's own total.

    The previous test proves the constructor call is assembled correctly (``agent.events is
    record_event``); it does not prove a critic's model turns actually land in a turn's record. This
    mirrors ``test_a_delegated_model_turn_lands_in_the_running_turns_record`` in
    ``tests/core/test_delegation.py``, which proves the identical thing for a spawned sub-agent: same
    mechanism, same two-call shape, because ``TurnMetrics.record`` only publishes a ``by_agent``
    breakdown once more than one distinct agent contributed, and a turn that asks for a review has
    still made at least one call of its own.
    """
    metrics = TurnMetrics()
    token = current_metrics.set(metrics)
    try:
        record_event(
            ModelTurnFinished(model="m1", usage={"input_tokens": 50, "output_tokens": 5}, duration_s=1.0, agent=None)
        )
        # What AIMU does from inside reviewer_agent's own client, once its events=record_event fires.
        record_event(
            ModelTurnFinished(
                model="m2", usage={"input_tokens": 300, "output_tokens": 40}, duration_s=1.5, agent="reviewer"
            )
        )
    finally:
        current_metrics.reset(token)
    record = metrics.record(wall_seconds=2.5)
    assert record["by_agent"]["reviewer"]["input_tokens"] == 300
