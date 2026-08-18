"""Frame-sequence characterization for every planned-turn mode.

These pin what a planned turn *does* -- the exact order of phases, cards, streamed calls, and the
committed transcript -- across all four combinations of show_reasoning x result_review, plus the
plan_review_agent and human-review branches.

They exist so the verbose and summary pipelines (two mirrored implementations of plan -> review ->
execute) can be collapsed into one without silently changing what the user sees. Written against the
pre-collapse behavior; any diff here after the collapse is a real behavior change, not noise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers import MockAsyncModelClient

from aimu.aio.channels.base import Channel, ChannelMessage
from aimu.models import StreamChunk, StreamingContentType
from kokua.core.assistant import Assistant
from kokua.config import AssistantConfig
from tests.channels import example_agents, planning_settings
from kokua.toolsets.planning import PLANNING_WORKFLOW
from kokua.workflows.critics import Verdict


class RecordingChannel(Channel):
    """A fully rich channel that records every frame in the order it arrives."""

    name = "fake"

    def __init__(self, *, phases: bool = True):
        self.events: list[tuple] = []
        if not phases:  # a channel that cannot render a verbose trace
            self.send_phase = None

    async def receive(self):
        if False:
            yield None

    async def send(self, content, *, reply_to=None) -> None:
        if isinstance(content, str):
            self.events.append(("send", content))
            return
        parts = [
            chunk.content
            async for chunk in content
            if chunk.phase == StreamingContentType.GENERATING and isinstance(chunk.content, str)
        ]
        self.events.append(("send_stream", "".join(parts)))

    async def stream_activity(self, chunks, *, show_answer=False) -> str:
        parts = [
            chunk.content
            async for chunk in chunks
            if chunk.phase == StreamingContentType.GENERATING and isinstance(chunk.content, str)
        ]
        text = "".join(parts)
        self.events.append(("stream", show_answer, text))
        return text

    async def send_phase(self, label, detail="") -> None:
        self.events.append(("phase", label, detail))

    async def send_subagent(self, event) -> None:
        self.events.append(("subagent", event["role"], event["status"]))

    async def send_plan(self, plan) -> None:
        self.events.append(("plan", plan))

    async def send_done(self) -> None:
        self.events.append(("done",))

    async def send_plan_review_request(self, plan, critique=None) -> None:
        self.events.append(("ask_plan_review", plan, critique))


def _planning(**overrides) -> dict[str, dict]:
    """This module's ``[planning]`` section: one replan/revise round, plus the flags a test sets."""
    return planning_settings(**{"review_rounds": 1, **overrides})


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {
        "data_dir": tmp_path,
        "agents": example_agents(),
        "entry_agent": "assistant",
        "toolset_settings": _planning(),
    }
    base.update(overrides)
    return AssistantConfig(**base)


async def _reviewer_stream(text: str):
    yield StreamChunk(StreamingContentType.GENERATING, text)


@pytest.fixture
def reviewers(monkeypatch):
    """Patch both reviewers to a scripted verdict sequence, streamed and non-streamed alike."""

    def install(plan_verdicts=(), result_verdicts=()):
        finalize = iter([*plan_verdicts, *result_verdicts])

        async def fake_stream_open(*args, **kwargs):
            return object(), _reviewer_stream("reviewer prose")

        async def fake_finalize(_client):
            return next(finalize)

        plans = iter(plan_verdicts)
        results = iter(result_verdicts)

        async def fake_review_plan(*args, **kwargs):
            return next(plans)

        async def fake_review_result(*args, **kwargs):
            return next(results)

        monkeypatch.setattr("kokua.workflows.planning.critics.stream_plan_review", fake_stream_open)
        monkeypatch.setattr("kokua.workflows.planning.critics.stream_result_review", fake_stream_open)
        monkeypatch.setattr("kokua.workflows.critics.finalize_verdict", fake_finalize)
        monkeypatch.setattr("kokua.workflows.planning.critics.review_plan", fake_review_plan)
        monkeypatch.setattr("kokua.workflows.planning.critics.review_result", fake_review_result)

    return install


APPROVE = Verdict(approved=True)
REJECT = Verdict(approved=False, issues=["needs work"])


async def _planned_turn(tmp_path, channel, replies, **planning):
    """Run one planned turn, with ``planning`` naming the ``[planning]`` flags this case turns on."""
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=_planning(**planning)), channel, client=MockAsyncModelClient(list(replies))
    )
    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
    )
    return assistant


def _transcript(assistant):
    return [(m["role"], m["content"]) for m in assistant._agent.model_client.messages if m.get("content")]


# --- the four show_reasoning x result_review combinations -----------------------------------------


async def test_summary_plain(tmp_path):
    """show_reasoning=False, result_review=False: plan card, then the answer streamed as the reply."""
    channel = RecordingChannel()
    assistant = await _planned_turn(tmp_path, channel, ["THE PLAN", "THE ANSWER"])

    assert channel.events == [
        ("stream", False, "THE PLAN"),  # the planner runs but its text is withheld...
        ("plan", "THE PLAN"),  # ...until it is shown as a plan card
        ("send_stream", "THE ANSWER"),  # the executor streams straight to the reply
    ]
    assert _transcript(assistant) == [("user", "do X"), ("assistant", "THE ANSWER")]


async def test_summary_with_result_review(tmp_path, reviewers):
    """result_review=True: the executor runs withheld, the reviewer shows a card, then the answer."""
    reviewers(result_verdicts=[APPROVE])
    channel = RecordingChannel()
    assistant = await _planned_turn(tmp_path, channel, ["THE PLAN", "THE ANSWER"], result_review=True)

    assert channel.events == [
        ("stream", False, "THE PLAN"),
        ("plan", "THE PLAN"),
        ("stream", False, "THE ANSWER"),  # withheld: the reviewer has not vetted it yet
        ("subagent", "Result reviewer", "running"),
        ("subagent", "Result reviewer", "approved"),
        ("send", "THE ANSWER"),  # shown only once vetted
    ]
    assert _transcript(assistant) == [("user", "do X"), ("assistant", "THE ANSWER")]


async def test_verbose_plain(tmp_path):
    """show_reasoning=True: every call streams live under a phase header, terminated by `done`."""
    channel = RecordingChannel()
    assistant = await _planned_turn(tmp_path, channel, ["THE PLAN", "THE ANSWER"], show_reasoning=True)

    assert channel.events == [
        ("phase", "Planner", "drafting a plan"),
        ("stream", True, "THE PLAN"),  # shown live, so no plan card follows
        ("phase", "Executor", "carrying out the plan"),
        ("stream", True, "THE ANSWER"),
        ("done",),
    ]
    assert _transcript(assistant) == [("user", "do X"), ("assistant", "THE ANSWER")]


async def test_verbose_with_result_review(tmp_path, reviewers):
    """Verbose overrides result_review's hide-until-vetted gate: every version is shown as it happens."""
    reviewers(result_verdicts=[REJECT, APPROVE])
    channel = RecordingChannel()
    assistant = await _planned_turn(
        tmp_path, channel, ["THE PLAN", "ANS1", "ANS2"], show_reasoning=True, result_review=True
    )

    assert channel.events == [
        ("phase", "Planner", "drafting a plan"),
        ("stream", True, "THE PLAN"),
        ("phase", "Executor", "carrying out the plan"),
        ("stream", True, "ANS1"),
        ("phase", "Result reviewer", "round 1"),
        ("stream", True, "reviewer prose"),  # the prose, not a card
        ("phase", "Executor", "revising the answer"),
        ("stream", True, "ANS2"),
        ("phase", "Result reviewer", "round 2"),
        ("stream", True, "reviewer prose"),
        ("done",),
    ]
    assert _transcript(assistant) == [("user", "do X"), ("assistant", "ANS2")]  # only the final version commits


# --- the adversarial plan reviewer ------------------------------------------------------------------


async def test_summary_plan_review_agent_replans_then_approves(tmp_path, reviewers):
    reviewers(plan_verdicts=[REJECT, APPROVE])
    channel = RecordingChannel()
    await _planned_turn(tmp_path, channel, ["PLAN1", "PLAN2", "ANSWER"], plan_review_agent=True)

    assert channel.events == [
        ("stream", False, "PLAN1"),
        ("subagent", "Plan reviewer", "running"),
        ("subagent", "Plan reviewer", "rejected"),
        ("stream", False, "PLAN2"),  # replanned
        ("subagent", "Plan reviewer", "running"),
        ("subagent", "Plan reviewer", "approved"),
        ("plan", "PLAN2"),
        ("send_stream", "ANSWER"),
    ]


async def test_summary_plan_review_agent_out_of_rounds_carries_concerns_into_the_card(tmp_path, reviewers):
    """Unresolved concerns ride along on the plan card rather than blocking the turn."""
    reviewers(plan_verdicts=[REJECT, REJECT])
    channel = RecordingChannel()
    await _planned_turn(tmp_path, channel, ["PLAN1", "PLAN2", "ANSWER"], plan_review_agent=True)

    (plan_event,) = [event for event in channel.events if event[0] == "plan"]
    assert "Reviewer's remaining concerns" in plan_event[1]
    assert "- needs work" in plan_event[1]


async def test_verbose_plan_review_agent_streams_prose_and_emits_no_cards(tmp_path, reviewers):
    reviewers(plan_verdicts=[REJECT, APPROVE])
    channel = RecordingChannel()
    await _planned_turn(tmp_path, channel, ["PLAN1", "PLAN2", "ANSWER"], plan_review_agent=True, show_reasoning=True)

    assert channel.events == [
        ("phase", "Planner", "drafting a plan"),
        ("stream", True, "PLAN1"),
        ("phase", "Plan reviewer", "round 1"),
        ("stream", True, "reviewer prose"),
        ("phase", "Planner", "revising the plan"),
        ("stream", True, "PLAN2"),
        ("phase", "Plan reviewer", "round 2"),
        ("stream", True, "reviewer prose"),
        ("phase", "Executor", "carrying out the plan"),
        ("stream", True, "ANSWER"),
        ("done",),
    ]
    assert not any(event[0] == "subagent" for event in channel.events)


# --- human plan review --------------------------------------------------------------------------


@pytest.mark.parametrize("verbose", [False, True], ids=["summary", "verbose"])
async def test_human_rejection_stops_the_turn_and_commits_nothing(tmp_path, verbose):
    channel = RecordingChannel()

    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=_planning(plan_review=True, show_reasoning=verbose)),
        channel,
        client=MockAsyncModelClient(["THE PLAN", "THE ANSWER"]),
    )

    import asyncio

    turn = asyncio.create_task(
        assistant._handle(
            ChannelMessage(text="do X", channel="fake"),
            conversation_id=assistant._active_id,
            workflow=PLANNING_WORKFLOW,
        )
    )
    for _ in range(1000):
        if assistant._human.decision.pending:
            assistant._human.decision.resolve(None)  # reject
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("plan review never became pending")
    await turn

    assert ("send", "(plan rejected)") in channel.events
    assert _transcript(assistant) == []  # nothing committed
    assert "trace" not in assistant._session.metadata  # no replay metadata for an uncommitted turn


# --- degradation ----------------------------------------------------------------------------------


async def test_show_reasoning_without_a_phase_channel_falls_back_to_summary(tmp_path):
    """A channel that cannot render phases must not take the verbose path, which never shows the answer."""
    channel = RecordingChannel(phases=False)
    await _planned_turn(tmp_path, channel, ["THE PLAN", "THE ANSWER"], show_reasoning=True)

    assert not any(event[0] == "phase" for event in channel.events)
    assert ("send_stream", "THE ANSWER") in channel.events
