"""A reviewer builds its own client outside any caller's scoped sink, so its cost has to be wired
in rather than threaded through as a parameter. See :func:`kokua.workflows.critics.reviewer_agent`."""

from kokua.core.metrics import record_event
from kokua.workflows.critics import reviewer_agent


def test_a_reviewer_reports_its_model_turns_to_the_metrics_forwarder():
    """A critic builds its own client, so without an explicit sink its cost is invisible.

    ``reviewer_agent`` takes no ``events`` parameter: the module-level forwarder is wired
    unconditionally, since it reads the running turn off a contextvar at emit time and needs no
    caller to pass it through.
    """
    agent = reviewer_agent(None, "review it")
    assert agent.events is record_event
