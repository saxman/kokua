"""Mock-only tests for adversarial plan + result review (monkeypatching the reviewer)."""

from __future__ import annotations

from pathlib import Path

from helpers import MockAsyncModelClient
from kokua import runtime_settings
from kokua.assistant import Assistant
from kokua.config import AssistantConfig
from kokua.review import Verdict

from aimu.aio.channels.base import Channel, ChannelMessage
from aimu.models import StreamingContentType


class FakeChannel(Channel):
    name = "fake"

    def __init__(self):
        self.sent: list = []  # (kind, text): "str" for a plain send, "stream" for a streamed send
        self.subagent: list = []  # sub-agent event dicts

    async def send_subagent(self, event) -> None:
        self.subagent.append(event)

    async def receive(self):
        if False:
            yield None

    async def send(self, content, *, reply_to=None) -> None:
        if isinstance(content, str):
            self.sent.append(("str", content))
            return
        parts = []
        async for chunk in content:
            if chunk.phase == StreamingContentType.GENERATING:
                parts.append(chunk.content)
        self.sent.append(("stream", "".join(parts)))


def _config(tmp_path: Path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "memory": False}
    base.update(overrides)
    return AssistantConfig(**base)


def _verdicts(seq, monkeypatch, which):
    """Monkeypatch review.review_plan/review_result to return the given Verdicts in order."""
    calls = {"n": 0}

    async def fake(*args, **kwargs):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v

    monkeypatch.setattr(f"kokua.review.{which}", fake)
    return calls


REJECT = Verdict(approved=False, issues=["missing a verification step"], suggestions="add checks")
APPROVE = Verdict(approved=True)


# --- reviewer primitive ---------------------------------------------------------------------


def test_verdict_defaults():
    v = Verdict(approved=True)
    assert v.issues == [] and v.suggestions == ""


# --- adversarial plan review ----------------------------------------------------------------


async def test_plan_review_replans_then_approves(tmp_path, monkeypatch):
    calls = _verdicts([REJECT, APPROVE], monkeypatch, "review_plan")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])  # plan, replan, execute
    assistant = await Assistant.create(
        _config(tmp_path, plan_mode=True, plan_review_agent=True), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    assert calls["n"] == 2  # reviewed twice (reject then approve)
    # The re-planned plan (PLAN2) was shown and executed; the answer came through.
    assert any(kind == "str" and "PLAN2" in text for kind, text in channel.sent)
    assert any("ANSWER" in text for _, text in channel.sent)


async def test_plan_review_exhausts_and_surfaces_critique(tmp_path, monkeypatch):
    _verdicts([REJECT], monkeypatch, "review_plan")  # always rejects
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])
    assistant = await Assistant.create(
        _config(tmp_path, plan_mode=True, plan_review_agent=True, review_rounds=1), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    # review_rounds=1 -> one replan, then proceed with the best plan plus surfaced concerns.
    assert any("remaining concerns" in text.lower() for kind, text in channel.sent if kind == "str")
    assert any("ANSWER" in text for _, text in channel.sent)


# --- adversarial result review --------------------------------------------------------------


async def test_result_review_revises_then_approves(tmp_path, monkeypatch):
    _verdicts([REJECT, APPROVE], monkeypatch, "review_result")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN", "ANS1", "ANS2"])  # plan, execute, revise
    assistant = await Assistant.create(_config(tmp_path, plan_mode=True, result_review=True), channel, client=client)

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    # Result review disables streaming: the answer arrives as a plain-string send, and it's the revised one.
    answer_sends = [text for kind, text in channel.sent if kind == "str" and "ANS" in text]
    assert answer_sends and "ANS2" in answer_sends[-1]
    # Clean history: the user's own words + the final answer.
    msgs = assistant._agent.model_client.messages
    assert msgs[-2] == {"role": "user", "content": "do X"}
    assert msgs[-1]["role"] == "assistant" and "ANS2" in msgs[-1]["content"]


async def test_result_review_exhausts_and_notes_issues(tmp_path, monkeypatch):
    _verdicts([REJECT], monkeypatch, "review_result")  # never approves
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN", "ANS1", "ANS2"])
    assistant = await Assistant.create(
        _config(tmp_path, plan_mode=True, result_review=True, review_rounds=1), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    assert any("unresolved issues" in text.lower() for kind, text in channel.sent if kind == "str")


# --- sub-agent display (frames + persistence) -----------------------------------------------


async def test_plan_review_emits_and_records_subagent(tmp_path, monkeypatch):
    _verdicts([REJECT, APPROVE], monkeypatch, "review_plan")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])
    assistant = await Assistant.create(
        _config(tmp_path, plan_mode=True, plan_review_agent=True), channel, client=client
    )

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    # Each round emits a running card then its verdict: reject (round 0), approve (round 1).
    assert [e["status"] for e in channel.subagent] == ["running", "rejected", "running", "approved"]
    assert all(e["role"] == "Plan reviewer" for e in channel.subagent)
    # Verdicts are recorded under the turn's user-message index for replay (no "running" persisted).
    recorded = [e for lst in assistant._session.metadata.get("subagent", {}).values() for e in lst]
    assert [e["status"] for e in recorded] == ["rejected", "approved"]


async def test_result_review_emits_and_records_subagent(tmp_path, monkeypatch):
    _verdicts([REJECT, APPROVE], monkeypatch, "review_result")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN", "ANS1", "ANS2"])
    assistant = await Assistant.create(_config(tmp_path, plan_mode=True, result_review=True), channel, client=client)

    await assistant._handle(ChannelMessage(text="do X", channel="fake"))

    assert [e["status"] for e in channel.subagent] == ["running", "rejected", "running", "approved"]
    assert all(e["role"] == "Result reviewer" for e in channel.subagent)
    assert assistant._session.metadata.get("subagent")  # recorded for replay


# --- settings -------------------------------------------------------------------------------


async def test_settings_carry_review_flags(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    s = assistant.current_settings()
    assert s["plan_review_agent"] is False and s["result_review"] is False

    await assistant.apply_settings({"plan_review_agent": True, "result_review": True, "generate_kwargs": {}})
    assert assistant._config.plan_review_agent is True and assistant._config.result_review is True


def test_sanitize_keeps_review_flags():
    result = runtime_settings.sanitize({"plan_review_agent": True, "result_review": False})
    assert result["plan_review_agent"] is True and result["result_review"] is False
