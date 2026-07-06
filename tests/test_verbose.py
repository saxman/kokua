"""Mock-only tests for the verbose trace (show_reasoning): every LLM call streams under a phase header."""

from __future__ import annotations

from pathlib import Path

from helpers import MockAsyncModelClient
from kokua.assistant import Assistant
from kokua.config import AssistantConfig
from kokua.review import Verdict

from aimu.aio.channels.base import Channel, ChannelMessage
from aimu.models import StreamChunk, StreamingContentType


class VerboseChannel(Channel):
    """A phase-capable fake channel (like WebChannel) recording phases, streamed calls, cards, done."""

    name = "fake"

    def __init__(self):
        self.sent: list = []
        self.phases: list = []
        self.subagent: list = []
        self.streamed: list = []  # (show_answer, text) per streamed call
        self.done = 0

    async def receive(self):
        if False:
            yield None

    async def send(self, content, *, reply_to=None) -> None:
        if isinstance(content, str):
            self.sent.append(content)
            return
        parts = []
        async for chunk in content:
            if chunk.phase == StreamingContentType.GENERATING and isinstance(chunk.content, str):
                parts.append(chunk.content)
        self.sent.append("".join(parts))

    async def stream_activity(self, chunks, *, show_answer=False) -> str:
        parts = []
        async for chunk in chunks:
            if chunk.phase == StreamingContentType.GENERATING and isinstance(chunk.content, str):
                parts.append(chunk.content)
        text = "".join(parts)
        self.streamed.append((show_answer, text))
        return text

    async def send_phase(self, label, detail="") -> None:
        self.phases.append((label, detail))

    async def send_subagent(self, event) -> None:
        self.subagent.append(event)

    async def send_done(self) -> None:
        self.done += 1


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False}
    base.update(overrides)
    return AssistantConfig(**base)


async def _fake_review_stream(text="reasoning"):
    yield StreamChunk(StreamingContentType.GENERATING, text)


def _patch_reviewer(monkeypatch, which, verdicts):
    """Monkeypatch review.stream_{which} + finalize_verdict to a fake stream and a verdict sequence."""
    seq = iter(verdicts)

    async def fake_open(*a, **k):
        return object(), _fake_review_stream(f"{which} review reasoning")

    async def fake_finalize(_client):
        return next(seq)

    monkeypatch.setattr(f"kokua.review.stream_{which}", fake_open)
    monkeypatch.setattr("kokua.review.finalize_verdict", fake_finalize)


REJECT = Verdict(approved=False, issues=["needs work"])
APPROVE = Verdict(approved=True)


async def test_verbose_no_reviewers_streams_phases_and_commits(tmp_path):
    channel = VerboseChannel()
    client = MockAsyncModelClient(["THE PLAN", "THE ANSWER"])  # planner, executor
    assistant = await Assistant.create(_config(tmp_path, show_reasoning=True), channel, client=client)

    await assistant._handle(ChannelMessage(text="do X", channel="fake"), plan=True)

    assert [label for label, _ in channel.phases] == ["Planner", "Executor"]
    assert all(show for show, _ in channel.streamed)  # every call streamed visibly (show_answer=True)
    assert channel.done == 1
    # Clean transcript: the user's words + the final answer (only the final pair is committed).
    msgs = assistant._agent.model_client.messages
    assert msgs[-2] == {"role": "user", "content": "do X"}
    assert msgs[-1] == {"role": "assistant", "content": "THE ANSWER"}
    # The raw trace is persisted for reload: each phase with its streamed text.
    segments = next(iter(assistant._session.metadata["trace"].values()))
    assert [(s["label"], s["text"]) for s in segments] == [("Planner", "THE PLAN"), ("Executor", "THE ANSWER")]


async def test_verbose_plan_review_streams_and_records_trace(tmp_path, monkeypatch):
    _patch_reviewer(monkeypatch, "plan_review", [REJECT, APPROVE])
    channel = VerboseChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])  # plan, replan, executor
    assistant = await Assistant.create(
        _config(tmp_path, plan_review_agent=True, show_reasoning=True), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"), plan=True)

    labels = [label for label, _ in channel.phases]
    assert labels.count("Plan reviewer") == 2  # reject then approve
    assert labels.count("Planner") == 2  # initial + one replan
    assert channel.subagent == []  # verbose mode shows the raw prose, not summary cards
    assert channel.done == 1
    # The full raw trace is persisted for reload (no summary verdicts).
    assert "subagent" not in assistant._session.metadata
    segments = next(iter(assistant._session.metadata["trace"].values()))
    assert [s["label"] for s in segments] == ["Planner", "Plan reviewer", "Planner", "Plan reviewer", "Executor"]
    # Each reviewer's streamed prose is captured (not just a verdict); the last phase holds the answer.
    reviewer_texts = [s["text"] for s in segments if s["label"] == "Plan reviewer"]
    assert reviewer_texts == ["plan_review review reasoning", "plan_review review reasoning"]
    assert segments[-1] == {"label": "Executor", "detail": "carrying out the plan", "text": "ANSWER"}


async def test_verbose_result_review_streams_every_version(tmp_path, monkeypatch):
    _patch_reviewer(monkeypatch, "result_review", [REJECT, APPROVE])
    channel = VerboseChannel()
    client = MockAsyncModelClient(["PLAN", "ANS1", "ANS2"])  # plan, execute, revise
    assistant = await Assistant.create(
        _config(tmp_path, result_review=True, show_reasoning=True), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"), plan=True)

    labels = [label for label, _ in channel.phases]
    assert labels.count("Result reviewer") == 2
    assert labels.count("Executor") == 2  # initial + one revision, both streamed (no gating)
    # Both answer versions were streamed live (show_answer=True), not withheld.
    streamed_texts = [text for _, text in channel.streamed]
    assert "ANS1" in streamed_texts and "ANS2" in streamed_texts
    msgs = assistant._agent.model_client.messages
    assert msgs[-1] == {"role": "assistant", "content": "ANS2"}
    # The persisted trace captures both executor versions and both reviewer rounds, so reload shows all.
    segments = next(iter(assistant._session.metadata["trace"].values()))
    assert [s["label"] for s in segments] == ["Planner", "Executor", "Result reviewer", "Executor", "Result reviewer"]
    assert [s["text"] for s in segments if s["label"] == "Executor"] == ["ANS1", "ANS2"]
    assert channel.subagent == []  # no summary cards in verbose mode


async def test_show_reasoning_without_phase_channel_uses_normal_path(tmp_path):
    # A channel with no send_phase must not take the verbose path (which would never show the answer).
    class PlainChannel(VerboseChannel):
        send_phase = None  # drop the capability

    channel = PlainChannel()
    client = MockAsyncModelClient(["THE PLAN", "THE ANSWER"])
    assistant = await Assistant.create(_config(tmp_path, show_reasoning=True), channel, client=client)

    await assistant._handle(ChannelMessage(text="do X", channel="fake"), plan=True)

    assert channel.phases == []  # verbose path skipped
    assert any("THE ANSWER" in s for s in channel.sent)  # answer still delivered via the normal path
