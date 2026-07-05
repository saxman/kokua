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
    assistant = await Assistant.create(_config(tmp_path, plan_mode=True, show_reasoning=True), channel, client=client)

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    assert [label for label, _ in channel.phases] == ["Planner", "Executor"]
    assert all(show for show, _ in channel.streamed)  # every call streamed visibly (show_answer=True)
    assert channel.done == 1
    # Clean transcript: the user's words + the final answer (intermediate trace not persisted).
    msgs = assistant._agent.model_client.messages
    assert msgs[-2] == {"role": "user", "content": "do X"}
    assert msgs[-1] == {"role": "assistant", "content": "THE ANSWER"}


async def test_verbose_plan_review_streams_and_replans(tmp_path, monkeypatch):
    _patch_reviewer(monkeypatch, "plan_review", [REJECT, APPROVE])
    channel = VerboseChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])  # plan, replan, executor
    assistant = await Assistant.create(
        _config(tmp_path, plan_mode=True, plan_review_agent=True, show_reasoning=True), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    labels = [label for label, _ in channel.phases]
    assert labels.count("Plan reviewer") == 2  # reject then approve
    assert labels.count("Planner") == 2  # initial + one replan
    assert [e["status"] for e in channel.subagent] == ["rejected", "approved"]
    assert channel.done == 1
    # Verdicts recorded for reload replay.
    assert assistant._session.metadata.get("subagent")


async def test_verbose_result_review_streams_every_version(tmp_path, monkeypatch):
    _patch_reviewer(monkeypatch, "result_review", [REJECT, APPROVE])
    channel = VerboseChannel()
    client = MockAsyncModelClient(["PLAN", "ANS1", "ANS2"])  # plan, execute, revise
    assistant = await Assistant.create(
        _config(tmp_path, plan_mode=True, result_review=True, show_reasoning=True), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    labels = [label for label, _ in channel.phases]
    assert labels.count("Result reviewer") == 2
    assert labels.count("Executor") == 2  # initial + one revision, both streamed (no gating)
    # Both answer versions were streamed live (show_answer=True), not withheld.
    streamed_texts = [text for _, text in channel.streamed]
    assert "ANS1" in streamed_texts and "ANS2" in streamed_texts
    msgs = assistant._agent.model_client.messages
    assert msgs[-1] == {"role": "assistant", "content": "ANS2"}


async def test_show_reasoning_without_phase_channel_uses_normal_path(tmp_path):
    # A channel with no send_phase must not take the verbose path (which would never show the answer).
    class PlainChannel(VerboseChannel):
        send_phase = None  # drop the capability

    channel = PlainChannel()
    client = MockAsyncModelClient(["THE PLAN", "THE ANSWER"])
    assistant = await Assistant.create(_config(tmp_path, plan_mode=True, show_reasoning=True), channel, client=client)

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    assert channel.phases == []  # verbose path skipped
    assert any("THE ANSWER" in s for s in channel.sent)  # answer still delivered via the normal path
