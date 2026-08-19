"""Mock-only tests for adversarial plan + result review (monkeypatching the reviewer).

The shared critic (``kokua.workflows.critics``) is covered here rather than in a module of its own
because every one of its behaviors is reached through planning's two reviewers: those are the only
prompts in the repository, so a test of the critic's tool loop or its typed verdict is a test of a
planned turn's review round as well, and splitting them would only move the mock setup.
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers import MockAsyncModelClient
from kokua.config.settings_sources import build_settings_table
from kokua.toolsets.planning import PLANNING_WORKFLOW
from kokua.workflows import critics
from kokua.workflows.planning import critics as review
from kokua.core.assistant import Assistant
from kokua.workflows.planning.runner import _tool_evidence
from kokua.config import AssistantConfig
from tests.channels import example_agents, planning_settings
from kokua.workflows.critics import Verdict

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
    base = {
        "data_dir": tmp_path,
        "agents": example_agents(),
        "entry_agent": "assistant",
        "toolset_settings": planning_settings(),
    }
    base.update(overrides)
    return AssistantConfig(**base)


def _verdicts(seq, monkeypatch, which):
    """Monkeypatch review.review_plan/review_result to return the given Verdicts in order."""
    calls = {"n": 0}

    async def fake(*args, **kwargs):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v

    monkeypatch.setattr(f"kokua.workflows.planning.critics.{which}", fake)
    return calls


REJECT = Verdict(approved=False, issues=["missing a verification step"], suggestions="add checks")
APPROVE = Verdict(approved=True)


# --- reviewer primitive ---------------------------------------------------------------------


def test_verdict_defaults():
    v = Verdict(approved=True)
    assert v.issues == [] and v.suggestions == ""


def test_reviewer_toolset_boundary():
    """The reviewer gets verification tools (date, web, arithmetic) but no access to user state."""
    names = {t.__name__ for t in critics.REVIEWER_TOOLS}
    # Present: the motivating date tool, web lookup, and arithmetic.
    assert {"get_current_date_and_time", "web_search", "get_webpage", "calculate"} <= names
    # Absent: the user's memory/documents, skill authoring, and MCP mutation.
    assert not (names & {"store_memory", "search_memories", "save_document", "search_documents"})
    assert not any(n in names for n in ("author_skill", "add_skill_script", "add_mcp_server", "remove_mcp_server"))


def test_reviewer_toolset_holds_nothing_the_approval_gate_would_have_to_cover():
    """A reviewer cannot be approval-gated (nobody to ask mid-review), so its toolset must contain no
    tool that needs a gate. Pinned against the shipped `confirm_tools` default rather than a literal
    list, so adding a name there fails here until the reviewer's toolset is re-checked."""
    names = {t.__name__ for t in critics.REVIEWER_TOOLS}
    assert not (names & set(AssistantConfig().confirm_tools))
    assert "execute_python" not in names  # the specific escape this guards: arbitrary code, unsandboxed


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
            "kokua.workflows.critics.reviewer_agent",
            lambda model, system, tools=None, **kwargs: aio.Agent(client, tools=[]),
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

    def fake_reviewer_agent(model, system, tools=None, **kwargs):
        return aio.Agent(client, tools=[])  # tools irrelevant: the mock fakes the tool round

    monkeypatch.setattr("kokua.workflows.critics.reviewer_agent", fake_reviewer_agent)

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
    monkeypatch.setattr(
        "kokua.workflows.critics.reviewer_agent",
        lambda model, system, tools=None, **kwargs: aio.Agent(client, tools=[]),
    )

    rc, stream = await review.stream_plan_review("mock", "do X", "PLAN")
    parts = [ch.content async for ch in stream if ch.phase == StreamingContentType.GENERATING]
    verdict = await critics.finalize_verdict(rc)

    assert "streamed assessment" in "".join(parts)
    assert any(m.get("role") == "tool" for m in client.messages)
    assert verdict.approved is False and verdict.issues == ["stale date"]


# --- adversarial plan review ----------------------------------------------------------------


async def test_plan_review_replans_then_approves(tmp_path, monkeypatch):
    calls = _verdicts([REJECT, APPROVE], monkeypatch, "review_plan")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])  # plan, replan, execute
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(plan_review_agent=True)), channel, client=client
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
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
        _config(tmp_path, toolset_settings=planning_settings(plan_review_agent=True, review_rounds=1)),
        channel,
        client=client,
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
    )

    # review_rounds=1 -> one replan, then proceed with the best plan plus surfaced concerns.
    assert any("remaining concerns" in text.lower() for kind, text in channel.sent if kind == "str")
    assert any("ANSWER" in text for _, text in channel.sent)


# --- adversarial result review --------------------------------------------------------------


async def test_result_review_revises_then_approves(tmp_path, monkeypatch):
    _verdicts([REJECT, APPROVE], monkeypatch, "review_result")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN", "ANS1", "ANS2"])  # plan, execute, revise
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(result_review=True)), channel, client=client
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
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
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(result_review=True, review_rounds=1)),
        channel,
        client=client,
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
    )

    assert any("unresolved issues" in text.lower() for kind, text in channel.sent if kind == "str")


async def test_result_review_receives_executor_evidence(tmp_path, monkeypatch):
    """The executor's tool transcript is extracted and passed to the result reviewer as evidence."""
    captured = {}

    async def fake_review_result(model, request, plan, answer, evidence="", **kwargs):
        captured["evidence"] = evidence
        return APPROVE

    monkeypatch.setattr("kokua.workflows.planning.critics.review_result", fake_review_result)
    channel = FakeChannel()
    # planning: PLAN; executor does a tool round ("tool" -> "ANS") then a continuation turn ("FINAL").
    client = MockAsyncModelClient(["PLAN", "tool", "ANS", "FINAL"])
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(result_review=True)), channel, client=client
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
    )

    # The evidence carries the executor's tool result (the mock's tool round), not just the final answer.
    assert "tool result" in captured["evidence"] and "mock_tool" in captured["evidence"]


# --- sub-agent display (frames + persistence) -----------------------------------------------


async def test_plan_review_emits_and_records_subagent(tmp_path, monkeypatch):
    _verdicts([REJECT, APPROVE], monkeypatch, "review_plan")
    channel = FakeChannel()
    client = MockAsyncModelClient(["PLAN1", "PLAN2", "ANSWER"])
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(plan_review_agent=True)), channel, client=client
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
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
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(result_review=True)), channel, client=client
    )

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=assistant._active_id, workflow=PLANNING_WORKFLOW
    )

    assert [e["status"] for e in channel.subagent] == ["running", "rejected", "running", "approved"]
    assert all(e["role"] == "Result reviewer" for e in channel.subagent)
    assert assistant._session.metadata.get("subagent")  # recorded for replay


# --- settings -------------------------------------------------------------------------------


async def test_settings_carry_review_flags(tmp_path):
    """The panel reaches a planning flag under its namespaced wire key, and applying one lands in the
    toolset's own section -- the same place the workflow reads it from."""
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    s = assistant.current_settings()
    assert s["planning.plan_review_agent"] is False and s["planning.result_review"] is False

    await assistant.apply_settings(
        {"planning.plan_review_agent": True, "planning.result_review": True, "generate_kwargs": {}}
    )
    section = assistant._config.toolset_settings["planning"]
    assert section["plan_review_agent"] is True and section["result_review"] is True


def test_sanitize_keeps_review_flags():
    """Through the live table, since these are the planning toolset's declarations rather than core
    entries: a core-only table would drop both keys as unknown."""
    result = build_settings_table().sanitize({"planning.plan_review_agent": True, "planning.result_review": False})
    assert result["planning.plan_review_agent"] is True and result["planning.result_review"] is False


# --- reviewer thinking effort ---------------------------------------------------------------


def test_reviewer_agent_carries_the_thinking_it_is_given(monkeypatch):
    """A reviewer is not an [agents.*] agent, so the [assistant] default is the only tier it has."""
    monkeypatch.setattr(aio, "client", lambda model, system=None: MockAsyncModelClient([]))
    assert critics.reviewer_agent(None, "Judge it.", thinking="high").thinking == "high"
    assert critics.reviewer_agent(None, "Judge it.", thinking=False).thinking is False
    assert critics.reviewer_agent(None, "Judge it.").thinking is None


async def test_the_configured_thinking_reaches_every_planning_reviewer(monkeypatch):
    """The default has to reach the reviewers through all four planning wrappers, not just the two
    non-streamed ones, or a verbose planned turn would review at a different effort than a quiet one."""

    class _StubAgent:
        def __init__(self):
            self.model_client = MockAsyncModelClient([])

        async def run(self, *args, **kwargs):
            return ""

    seen: list = []

    def fake_reviewer(model, system, tools=None, thinking=None):
        seen.append(thinking)
        return _StubAgent()

    async def fake_finalize(client):
        return APPROVE

    monkeypatch.setattr(critics, "reviewer_agent", fake_reviewer)
    monkeypatch.setattr(critics, "finalize_verdict", fake_finalize)

    await review.review_plan(None, "req", "plan", thinking="high")
    await review.review_result(None, "req", "plan", "answer", thinking="high")
    await review.stream_plan_review(None, "req", "plan", thinking="high")
    await review.stream_result_review(None, "req", "plan", "answer", thinking="high")

    assert seen == ["high"] * 4
