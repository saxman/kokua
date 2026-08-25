"""TurnMetrics: what a turn cost, accumulated from AIMU's run events."""

from aimu.events import ContextCompacted, ModelTurnFinished, ModelTurnStarted

from kokua.core.metrics import TurnMetrics, current_metrics, record_event


def _finished(model="m", input_tokens=None, output_tokens=None, duration_s=1.0, agent=None):
    usage = None
    if input_tokens is not None or output_tokens is not None:
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    return ModelTurnFinished(model=model, usage=usage, duration_s=duration_s, agent=agent)


def test_counts_calls_and_sums_tokens_and_seconds():
    metrics = TurnMetrics()
    metrics(_finished(input_tokens=100, output_tokens=10, duration_s=1.5))
    metrics(_finished(input_tokens=200, output_tokens=20, duration_s=2.5))
    record = metrics.record(wall_seconds=5.0)
    assert record["calls"] == 2
    assert record["input_tokens"] == 300
    assert record["output_tokens"] == 30
    assert record["model_seconds"] == 4.0
    assert record["wall_seconds"] == 5.0


def test_unreported_usage_leaves_the_keys_absent_rather_than_zero():
    """A provider reporting no usage must not be recorded as having used none.

    A stored zero becomes a rendered zero becomes a false claim about the run, and the local
    OpenAI-compatible endpoints Kokua is often pointed at are exactly the ones that may report
    nothing. Absent is the honest value, and the renderer says "not reported".
    """
    metrics = TurnMetrics()
    metrics(_finished(duration_s=1.0))
    record = metrics.record(wall_seconds=2.0)
    assert record["calls"] == 1
    assert "input_tokens" not in record
    assert "output_tokens" not in record


def test_partially_reported_usage_counts_only_what_was_reported():
    """One call reporting and one not gives the reported figure, not a total presented as complete."""
    metrics = TurnMetrics()
    metrics(_finished(input_tokens=100, output_tokens=10))
    metrics(_finished())
    record = metrics.record(wall_seconds=1.0)
    assert record["calls"] == 2
    assert record["input_tokens"] == 100
    assert record["reported_calls"] == 1


def test_collects_distinct_models_in_first_seen_order():
    metrics = TurnMetrics()
    metrics(_finished(model="a"))
    metrics(_finished(model="b"))
    metrics(_finished(model="a"))
    assert metrics.record(wall_seconds=1.0)["models"] == ["a", "b"]


def test_attributes_per_agent_when_more_than_one_reported():
    """A delegated turn's cost splits by the events' agent field, which mirrors StreamChunk's."""
    metrics = TurnMetrics()
    metrics(_finished(input_tokens=100, output_tokens=10, agent=None))
    metrics(_finished(input_tokens=900, output_tokens=90, agent="subagent-research"))
    record = metrics.record(wall_seconds=1.0)
    assert record["input_tokens"] == 1000
    assert record["by_agent"]["subagent-research"]["input_tokens"] == 900


def test_omits_by_agent_when_only_the_entry_agent_ran():
    """A breakdown of one is noise in every export of an undelegated turn."""
    metrics = TurnMetrics()
    metrics(_finished(input_tokens=100, output_tokens=10))
    assert "by_agent" not in metrics.record(wall_seconds=1.0)


def test_ignores_events_it_does_not_measure():
    """AIMU's events are a dataclass union so a new member cannot break a consumer, which means
    ignoring the unrecognized is the correct default rather than a gap."""
    metrics = TurnMetrics()
    metrics(ModelTurnStarted())
    metrics(ContextCompacted())
    metrics(_finished(input_tokens=5, output_tokens=1))
    assert metrics.record(wall_seconds=1.0)["calls"] == 1


def test_a_turn_that_made_no_model_call_records_nothing_worth_storing():
    """`record` answers None so the caller can skip the write entirely rather than store an empty
    dict that renders as a turn which somehow cost nothing."""
    assert TurnMetrics().record(wall_seconds=1.0) is None


def test_the_forwarder_reaches_the_turns_sink():
    metrics = TurnMetrics()
    token = current_metrics.set(metrics)
    try:
        record_event(_finished(input_tokens=7, output_tokens=1))
    finally:
        current_metrics.reset(token)
    assert metrics.record(wall_seconds=1.0)["input_tokens"] == 7


def test_the_forwarder_is_a_no_op_outside_a_turn():
    """Every seam is wired with the forwarder once, at build time, so it is called with no turn in
    progress routinely. That has to be silent rather than an error."""
    record_event(_finished(input_tokens=7))  # must not raise


def test_concurrent_turns_do_not_share_a_record():
    """The isolation the ContextVar exists for. A shared field would let one conversation's turn
    accumulate into another's, which is the bug core/subagents.py already avoids this same way."""
    import asyncio

    async def one_turn(tokens):
        metrics = TurnMetrics()
        token = current_metrics.set(metrics)
        try:
            record_event(_finished(input_tokens=tokens))
            await asyncio.sleep(0)  # yield, so the two turns interleave
            record_event(_finished(input_tokens=tokens))
            return metrics.record(wall_seconds=1.0)["input_tokens"]
        finally:
            current_metrics.reset(token)

    async def both():
        return await asyncio.gather(one_turn(10), one_turn(100))

    assert asyncio.run(both()) == [20, 200]
