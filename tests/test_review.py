"""Mock-only tests for adversarial plan + result review (monkeypatching the reviewer)."""

from __future__ import annotations

from pathlib import Path

from helpers import MockAsyncModelClient
from kokua import review, runtime_settings
from kokua.assistant import Assistant
from kokua.planning import _tool_evidence
from kokua.config import AssistantConfig
from kokua.review import Verdict

from aimu import aio
from aimu.aio.channels.base import Channel, ChannelMessage
from aimu.models import StreamingContentType


class FakeChannel(Channel):
    name = "fake"

    def __init__(self):
        self.sent: list = []  # (kind, text): "str" for a plain send, "stream" for a streamed send
        self.subagent: list = []  # sub-agent event dicts

    async def send_subagent(self, event) -> None:
        self.subagent.append(event)

    async def stream_activity(self, chunks, *, show_answer=False) -> str:
        # Mirror WebChannel: accumulate GENERATING (the answer) and return it; loop frames are display-only.
        parts = []
        async for chunk in chunks:
            if chunk.phase == StreamingContentType.GENERATING and isinstance(chunk.content, str):
                parts.append(chunk.content)
        return "".join(parts)

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


def test_reviewer_toolset_boundary():
    """The reviewer gets verification tools (date, web, compute) but no access to user state."""
    names = {t.__name__ for t in review.REVIEWER_TOOLS}
    # Present: the motivating date tool, web lookup, and computation (incl. execute_python for math).
    assert {"get_current_date_and_time", "web_search", "get_webpage", "calculate", "execute_python"} <= names
    # Absent: the user's memory/documents, skill authoring, and MCP mutation.
    assert not (names & {"store_memory", "search_memories", "save_document", "search_documents"})
    assert not any(n in names for n in ("author_skill", "add_skill_script", "add_mcp_server", "remove_mcp_server"))


def test_reviewer_prompts_warn_about_stale_knowledge():
    """Both reviewers are told to verify with tools before flagging; only the result reviewer sees evidence."""
    assert "out of date" in review.PLAN_REVIEW_SYSTEM and "verify" in review.PLAN_REVIEW_SYSTEM.lower()
    assert "out of date" in review.RESULT_REVIEW_SYSTEM
    assert "Evidence section" in review.RESULT_REVIEW_SYSTEM  # evidence guidance
    assert "Evidence section" not in review.PLAN_REVIEW_SYSTEM


def test_tool_evidence_renders_and_truncates():
    """_tool_evidence renders tool results (labeled by call name), truncates long ones, and skips no-tool runs."""
    messages = [
        {"role": "assistant", "tool_calls": [{"function": {"name": "web_search", "arguments": {}}, "id": "a1"}]},
        {"role": "tool", "content": "FRESH-DATA", "tool_call_id": "a1"},  # name resolved via the call id
        {"role": "assistant", "content": "the answer"},
    ]
    evidence = _tool_evidence(messages)
    assert evidence == "- web_search: FRESH-DATA"
    assert _tool_evidence([{"role": "assistant", "content": "no tools used"}]) == ""
    truncated = _tool_evidence([{"role": "tool", "name": "t", "content": "y" * 100}], max_chars=10)
    assert truncated == "- t: " + "y" * 10 + " ...[truncated]"


async def test_review_result_includes_evidence_in_prompt(monkeypatch):
    """review_result threads evidence into the reviewer's user message; the default omits the block."""
    for evidence, expect in [("SRC-XYZ", True), ("", False)]:
        client = MockAsyncModelClient(["assessment", '{"approved": true, "issues": [], "suggestions": ""}'])
        client.model.supports_structured_output = False
        monkeypatch.setattr(
            "kokua.review._reviewer_agent", lambda model, system, tools=None: aio.Agent(client, tools=[])
        )
        await review.review_result("mock", "do X", "PLAN", "ANSWER", evidence)
        first_user = client.messages[0]["content"]
        assert ("Evidence the agent gathered" in first_user) is expect
        assert ("SRC-XYZ" in first_user) is expect


async def test_reviewer_runs_tool_loop_then_extracts_verdict(monkeypatch):
    """A reviewer runs a bounded tool-calling assessment, then finalize_verdict parses the typed verdict."""
    # The mock's "tool" entry is one tool round: the tool call plus the follow-up prose. finalize_verdict
    # then makes one more (structured) call for the typed verdict.
    client = MockAsyncModelClient(
        ["tool", "prose after the tool call", '{"approved": true, "issues": [], "suggestions": ""}']
    )
    client.model.supports_structured_output = False  # route the verdict through the parse path

    def fake_reviewer_agent(model, system, tools=None):
        return aio.Agent(client, tools=[])  # tools irrelevant: the mock fakes the tool round

    monkeypatch.setattr("kokua.review._reviewer_agent", fake_reviewer_agent)

    verdict = await review.review_plan("mock", "do X", "PLAN")

    assert verdict.approved is True
    # The reviewer actually exercised a tool round before verdicting (not a single tool-less call).
    assert any(m.get("role") == "tool" for m in client.messages)


async def test_streamed_reviewer_streams_then_extracts_verdict(monkeypatch):
    """The streamed reviewer yields its assessment chunks, then finalize_verdict returns the verdict.

    Guards the two-phase streamed path (``stream_*`` must ``await agent.run(stream=True)`` to get an
    async iterator, then ``finalize_verdict`` on the same client)."""
    client = MockAsyncModelClient(
        ["tool", "streamed assessment", '{"approved": false, "issues": ["stale date"], "suggestions": ""}']
    )
    client.model.supports_structured_output = False
    monkeypatch.setattr("kokua.review._reviewer_agent", lambda model, system, tools=None: aio.Agent(client, tools=[]))

    rc, stream = await review.stream_plan_review("mock", "do X", "PLAN")
    parts = [ch.content async for ch in stream if ch.phase == StreamingContentType.GENERATING]
    verdict = await review.finalize_verdict(rc)

    assert "streamed assessment" in "".join(parts)
    assert any(m.get("role") == "tool" for m in client.messages)
    assert verdict.approved is False and verdict.issues == ["stale date"]


# --- adversarial plan review ----------------------------------------------------------------


async def test_plan_review_replans_then_approves(tmp_path, monkeypatch):
    calls = _verdicts([REJECT, APPROVE], monkeypatch, "review_plan")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])  # plan, replan, execute
    assistant = await Assistant.create(_config(tmp_path, plan_review_agent=True), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, plan=True
    )

    assert calls["n"] == 2  # reviewed twice (reject then approve)
    # The re-planned plan (PLAN2) was shown and executed; the answer came through.
    assert any(kind == "str" and "PLAN2" in text for kind, text in channel.sent)
    assert any("ANSWER" in text for _, text in channel.sent)


async def test_plan_review_exhausts_and_surfaces_critique(tmp_path, monkeypatch):
    _verdicts([REJECT], monkeypatch, "review_plan")  # always rejects
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])
    assistant = await Assistant.create(
        _config(tmp_path, plan_review_agent=True, review_rounds=1), channel, client=client
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, plan=True
    )

    # review_rounds=1 -> one replan, then proceed with the best plan plus surfaced concerns.
    assert any("remaining concerns" in text.lower() for kind, text in channel.sent if kind == "str")
    assert any("ANSWER" in text for _, text in channel.sent)


# --- adversarial result review --------------------------------------------------------------


async def test_result_review_revises_then_approves(tmp_path, monkeypatch):
    _verdicts([REJECT, APPROVE], monkeypatch, "review_result")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN", "ANS1", "ANS2"])  # plan, execute, revise
    assistant = await Assistant.create(_config(tmp_path, result_review=True), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, plan=True
    )

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
    assistant = await Assistant.create(_config(tmp_path, result_review=True, review_rounds=1), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, plan=True
    )

    assert any("unresolved issues" in text.lower() for kind, text in channel.sent if kind == "str")


async def test_result_review_receives_executor_evidence(tmp_path, monkeypatch):
    """The executor's tool transcript is extracted and passed to the result reviewer as evidence."""
    captured = {}

    async def fake_review_result(model, request, plan, answer, evidence=""):
        captured["evidence"] = evidence
        return APPROVE

    monkeypatch.setattr("kokua.review.review_result", fake_review_result)
    channel = FakeChannel()
    # planning: PLAN; executor does a tool round ("tool" -> "ANS") then a continuation turn ("FINAL").
    client = MockAsyncModelClient(["PLAN", "tool", "ANS", "FINAL"])
    assistant = await Assistant.create(_config(tmp_path, result_review=True), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, plan=True
    )

    # The evidence carries the executor's tool result (the mock's tool round), not just the final answer.
    assert "tool result" in captured["evidence"] and "mock_tool" in captured["evidence"]


# --- sub-agent display (frames + persistence) -----------------------------------------------


async def test_plan_review_emits_and_records_subagent(tmp_path, monkeypatch):
    _verdicts([REJECT, APPROVE], monkeypatch, "review_plan")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])
    assistant = await Assistant.create(_config(tmp_path, plan_review_agent=True), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, plan=True
    )

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
    assistant = await Assistant.create(_config(tmp_path, result_review=True), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, plan=True
    )

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
