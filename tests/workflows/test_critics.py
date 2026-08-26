"""A reviewer builds its own client outside any caller's scoped sink, so its cost has to be wired
in rather than threaded through as a parameter. See :func:`kokua.workflows.critics.reviewer_agent`.

Coverage of the critic seam splits into three links, none of them proven by a single test:

1. Kokua wires ``reviewer_agent`` to the forwarder. Pinned by
   ``test_a_reviewer_reports_its_model_turns_to_the_metrics_forwarder`` below, which calls
   ``reviewer_agent`` for real and asserts ``agent.events is record_event``; severing the wiring
   (``events=None``) fails that assertion.
2. AIMU delivers a sink passed as ``aio.Agent(events=...)`` to the agent's own model turns. Pinned by
   AIMU's own suite, not this one, since Kokua has no reason to re-prove a library capability it depends on.
3. ``record_event`` routes to whichever ``TurnMetrics`` the running turn opened, and attributes by the
   event's ``agent`` field rather than folding it into the entry agent's total. Pinned by
   ``test_the_forwarder_attributes_a_reviewers_cost_to_the_reviewer`` below, which calls
   ``record_event`` directly with hand-built events; it does not go through ``reviewer_agent`` or a
   real model call, so it says nothing about links 1 or 2.

``reviewer_agent`` builds its client internally with ``aio.client(...)`` and offers no seam to inject a
fake one, so a single test spanning all three links would mean contorting that function for
injectability to re-cover ground these three already cover between them. What none of the three catches:
someone changing the wiring in (1) and its own test in the same change would not be caught by (3), since
(3) never touches ``reviewer_agent`` at all. That composition is accepted, not closed, deliberately.
"""

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


def test_the_forwarder_attributes_a_reviewers_cost_to_the_reviewer():
    """``record_event`` routes to whichever accumulator the running turn opened, and ``TurnMetrics``
    attributes by the event's ``agent`` field rather than folding it into the entry agent's total.

    This calls ``record_event`` directly with a hand-built event; it does not go through
    ``reviewer_agent`` or a real model call, so it proves the routing and attribution half of the
    critic seam, not the wiring (see the previous test) or AIMU's own delivery of ``events=`` to a
    model turn (see the module docstring for where each link is actually pinned).

    Two calls, not one: ``TurnMetrics.record`` only publishes a ``by_agent`` breakdown once more than
    one distinct agent contributed, and a turn that asks for a review has still made at least one call
    of its own.
    """
    metrics = TurnMetrics()
    token = current_metrics.set(metrics)
    try:
        record_event(
            ModelTurnFinished(model="m1", usage={"input_tokens": 50, "output_tokens": 5}, duration_s=1.0, agent=None)
        )
        # A stand-in for what a reviewer's own model turn would carry, once AIMU delivers
        # reviewer_agent's events=record_event to it.
        record_event(
            ModelTurnFinished(
                model="m2", usage={"input_tokens": 300, "output_tokens": 40}, duration_s=1.5, agent="reviewer"
            )
        )
    finally:
        current_metrics.reset(token)
    record = metrics.record(wall_seconds=2.5)
    assert record["by_agent"]["reviewer"]["input_tokens"] == 300
