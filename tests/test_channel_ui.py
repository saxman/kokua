"""The ChannelUI degradation matrix.

This is the contract that lets the core stop asking ``getattr(channel, "send_x", None)`` at seventeen
call sites: every optional frame has exactly one documented fallback, asserted here for a bare
``Channel`` and for a fully rich one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest
from aimu.models import StreamChunk, StreamingContentType

from kokua.channels.ui import ChannelUI


class BareChannel:
    """The minimum a transport must implement: AIMU's base Channel contract."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content, *, reply_to=None) -> None:
        self.sent.append(content)

    def receive(self):
        raise NotImplementedError


class _TurnSavedDouble(BareChannel):
    """Records what reached ``send_turn_saved``, token included."""

    def __init__(self):
        super().__init__()
        self.saved: list[tuple[str, int, Optional[str]]] = []

    async def send_turn_saved(self, conversation_id, message_index, *, token=None, turn=None):
        self.saved.append((conversation_id, message_index, token))


class RichChannelDouble(BareChannel):
    """Every optional frame, recorded rather than rendered."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, tuple]] = []
        self.active_conversation_id: Optional[str] = None

    async def send_conversations(self, items: list[dict]) -> None:
        self.calls.append(("conversations", (items,)))

    async def send_notification(self, text: str, *, conversation_id=None, url=None, group=None) -> None:
        self.calls.append(("notification", (text, conversation_id, url, group)))

    async def send_approval_request(self, name: str, arguments: Any, *, agent: Optional[str] = None) -> None:
        self.calls.append(("approval", (name, arguments, agent)))

    async def send_auto_approval(
        self, name: str, arguments: Any, *, approved: bool, reason: str, model: str, conversation_id=None, agent=None
    ) -> None:
        self.calls.append(("auto_approval", (name, arguments, approved, reason, model, conversation_id, agent)))

    async def send_plan_review_request(self, plan: str, critique: Optional[str] = None) -> None:
        self.calls.append(("plan_review", (plan, critique)))

    async def send_plan(self, plan: str) -> None:
        self.calls.append(("plan", (plan,)))

    async def send_phase(self, label: str, detail: str = "") -> None:
        self.calls.append(("phase", (label, detail)))

    async def send_done(self) -> None:
        self.calls.append(("done", ()))

    async def send_subagent(self, event: dict) -> None:
        self.calls.append(("subagent", (event,)))

    async def send_artifact(self, record: dict) -> None:
        self.calls.append(("artifact", (record,)))

    async def send_request(self, record: dict) -> None:
        self.calls.append(("request", (record,)))

    async def send_ending(self, ending: dict) -> None:
        self.calls.append(("ending", (ending,)))

    async def send_message_frame(self, text: str, *, token: Optional[str] = None) -> None:
        self.calls.append(("message_frame", (text, token)))

    async def stream_activity(self, chunks: AsyncIterator, *, show_answer: bool = False) -> str:
        parts = [chunk async for chunk in chunks]
        self.calls.append(("stream_activity", (show_answer,)))
        return "".join(parts)

    def begin_catch_up(self, conversation_id: str, text: str, image_paths: Optional[list[str]] = None) -> None:
        self.calls.append(("begin_catch_up", (conversation_id, text, image_paths)))

    def end_catch_up(self, conversation_id: str) -> None:
        self.calls.append(("end_catch_up", (conversation_id,)))


async def _chunks(*items):
    for item in items:
        yield item


# --- capability probing -------------------------------------------------------------------------


def test_bare_channel_advertises_no_capabilities():
    ui = ChannelUI(BareChannel())
    assert ui.supports_conversations is False
    assert ui.supports_phases is False
    assert ui.supports_streamed_activity is False


def test_rich_channel_advertises_every_capability():
    ui = ChannelUI(RichChannelDouble())
    assert ui.supports_conversations is True
    assert ui.supports_phases is True
    assert ui.supports_streamed_activity is True


# --- degradations on a bare channel -------------------------------------------------------------


async def test_conversations_and_notify_are_skipped_silently():
    channel = BareChannel()
    ui = ChannelUI(channel)
    await ui.push_conversations([{"id": "a"}])
    await ui.notify("done")
    assert channel.sent == []  # no sidebar and no background turns: nothing to say


async def test_phase_subagent_and_finish_are_no_ops():
    channel = BareChannel()
    ui = ChannelUI(channel)
    await ui.show_phase("Planner", "drafting")
    await ui.show_subagent({"role": "Plan reviewer", "status": "running"})
    await ui.finish_stream()
    assert channel.sent == []


async def test_a_loop_entry_reaches_a_card_channel_and_is_a_no_op_without_one():
    """The entry is a card `append` like any other, so it needs no route of its own: a channel that
    renders cards gets it, one that does not (the terminal) drops it silently."""
    entry = {"id": "r-1", "append": {"kind": "loop", "reason": "continuation", "text": "Keep going."}}

    rich = RichChannelDouble()
    await ChannelUI(rich).show_subagent(entry)
    assert rich.calls[-1] == ("subagent", (entry,))

    bare = BareChannel()
    await ChannelUI(bare).show_subagent(entry)
    assert bare.sent == []


async def test_approval_falls_back_to_a_text_question():
    channel = BareChannel()
    await ChannelUI(channel).ask_approval("execute_python", {"code": "1"})
    assert channel.sent == ["[approve] Allow execute_python({'code': '1'})? [y/N]"]


async def test_plain_text_approval_names_the_agent():
    channel = BareChannel()
    await ChannelUI(channel).ask_approval("run_command", {"command": "ls"}, agent="coder")
    assert channel.sent[-1] == "[approve] Allow coder to run run_command({'command': 'ls'})? [y/N]"


async def test_rich_approval_carries_the_agent_and_defaults_to_none():
    channel = RichChannelDouble()
    ui = ChannelUI(channel)
    await ui.ask_approval("run_command", {}, agent="coder")
    await ui.ask_approval("run_command", {})
    assert channel.calls == [("approval", ("run_command", {}, "coder")), ("approval", ("run_command", {}, None))]


async def test_plan_review_falls_back_to_text_and_includes_the_critique():
    channel = BareChannel()
    await ChannelUI(channel).ask_plan_review("step 1", ["too vague", "no verification"])
    (text,) = channel.sent
    assert "approve" in text and "reject" in text and "edit:" in text
    assert "- too vague" in text and "- no verification" in text


async def test_plan_review_without_a_critique_omits_the_concerns_block():
    channel = BareChannel()
    await ChannelUI(channel).ask_plan_review("step 1")
    assert "concerns" not in channel.sent[0].lower()


async def test_plan_falls_back_to_a_labeled_message():
    channel = BareChannel()
    await ChannelUI(channel).show_plan("step 1")
    assert channel.sent == ["Plan:\n\nstep 1"]


async def test_stream_activity_drains_and_returns_empty_without_streaming():
    """The run must complete even when nobody can watch it; the text is simply unavailable."""
    channel = BareChannel()
    stream = _chunks("a", "b")
    assert await ChannelUI(channel).stream_activity(stream) == ""
    assert [chunk async for chunk in stream] == []  # fully drained


def test_mirrored_attributes_are_no_ops_without_them():
    ui = ChannelUI(BareChannel())
    ui.set_active_conversation("c1")  # no error


# --- pass-through on a rich channel ---------------------------------------------------------------


async def test_rich_channel_receives_every_frame():
    channel = RichChannelDouble()
    ui = ChannelUI(channel)
    await ui.push_conversations([{"id": "a"}])
    await ui.notify("done")
    await ui.alert("a scheduled task finished")
    await ui.ask_approval("execute_python", {"code": "1"})
    await ui.show_auto_approval("run_command", {"command": "ls"}, approved=True, reason="lists files", model="m")
    await ui.ask_plan_review("step 1", ["too vague"])
    await ui.show_plan("step 1")
    await ui.show_phase("Planner", "drafting")
    await ui.show_subagent({"role": "Plan reviewer"})
    await ui.show_request({"run_id": "r"})
    await ui.show_ending({"run_id": "r"})
    await ui.finish_stream()
    assert await ui.stream_activity(_chunks("a", "b"), show_answer=True) == "ab"

    assert [name for name, _ in channel.calls] == [
        "conversations",
        "notification",  # notify
        "notification",  # alert: one frame, two methods, differing only in their fallback
        "approval",
        "auto_approval",
        "plan_review",
        "plan",
        "phase",
        "subagent",
        "request",
        "ending",
        "done",
        "stream_activity",
    ]
    assert channel.sent == []  # a rich channel never falls back to plain text


async def test_plan_review_critique_reaches_a_rich_channel_as_rendered_bullets():
    channel = RichChannelDouble()
    await ChannelUI(channel).ask_plan_review("step 1", ["too vague", "no verification"])
    (_, (plan, critique)) = channel.calls[0]
    assert plan == "step 1"
    assert critique == "- too vague\n- no verification"


def test_mirrored_attributes_reach_a_rich_channel():
    channel = RichChannelDouble()
    ui = ChannelUI(channel)
    ui.set_active_conversation("c1")
    ui.begin_catch_up("c1", "hi", ["/tmp/a.png"])
    ui.end_catch_up("c1")
    assert channel.active_conversation_id == "c1"
    assert channel.calls == [("begin_catch_up", ("c1", "hi", ["/tmp/a.png"])), ("end_catch_up", ("c1",))]


def test_catch_up_bookkeeping_is_a_no_op_on_a_bare_channel():
    """A transport showing one conversation has nothing to catch up on, so the core's calls must be
    harmless rather than guarded at each call site."""
    ui = ChannelUI(BareChannel())
    ui.begin_catch_up("c1", "hi", None)
    ui.end_catch_up("c1")


async def test_turn_saved_is_a_no_op_on_a_channel_that_cannot_take_it():
    ui = ChannelUI(BareChannel())
    await ui.turn_saved("abc123", 4)  # no frame to send, and no error either


async def test_turn_saved_reaches_a_channel_that_offers_it():
    channel = _TurnSavedDouble()
    await ChannelUI(channel).turn_saved("abc123", 4)
    assert channel.saved == [("abc123", 4, None)]


async def test_turn_saved_carries_the_bubble_token_the_front_end_minted():
    """The front end matches the message it drew to the turn it became, so the token it sent with the
    message has to reach the frame that reports the turn."""
    channel = _TurnSavedDouble()
    await ChannelUI(channel).turn_saved("abc123", 4, token="b9")
    assert channel.saved == [("abc123", 4, "b9")]


# --- message_taken: the other fate a message can meet -----------------------------------------------


async def test_message_taken_is_a_no_op_on_a_channel_that_cannot_take_it():
    """A transport that draws no message of its own has nothing to go back and mark."""
    await ChannelUI(BareChannel()).message_taken("use the cache", token="b9")


async def test_message_taken_reaches_a_channel_that_offers_it():
    channel = RichChannelDouble()
    await ChannelUI(channel).message_taken("use the cache", token="b9")
    assert channel.calls == [("message_frame", ("use the cache", "b9"))]


# --- alerts: a card the user has to dismiss -------------------------------------------------------


async def test_alert_carries_its_link_to_a_rich_channel():
    """An alert names what it is about, so the card can offer a way to act on it."""
    channel = RichChannelDouble()
    await ChannelUI(channel).alert("Scheduled task 'Digest' finished.", conversation_id="c7")
    assert channel.calls == [("notification", ("Scheduled task 'Digest' finished.", "c7", None, None))]


async def test_alert_carries_an_authorization_url():
    channel = RichChannelDouble()
    await ChannelUI(channel).alert("Authorize access.", url="https://example.test/auth")
    assert channel.calls == [("notification", ("Authorize access.", None, "https://example.test/auth", None))]


async def test_alert_falls_back_to_plain_text():
    """Unlike ``notify``, an alert must land somewhere: the terminal prints the sentence, which is
    self-contained precisely so this fallback loses nothing but the click."""
    channel = BareChannel()
    await ChannelUI(channel).alert("Authorize access at https://example.test/auth", url="https://example.test/auth")
    assert channel.sent == ["Authorize access at https://example.test/auth"]


async def test_notify_names_the_conversation_it_is_about():
    channel = RichChannelDouble()
    await ChannelUI(channel).notify("Reply ready in 'Digest'.", conversation_id="c7")
    assert channel.calls == [("notification", ("Reply ready in 'Digest'.", "c7", None, "c7"))]


async def test_alert_carries_the_group_that_supersedes_an_earlier_card():
    """An always-on assistant fires the same task all night. Without a group, a user who was away
    comes back to one card per firing; with it, the newest card for a group is the only one left."""
    channel = RichChannelDouble()
    await ChannelUI(channel).alert("Scheduled task 'Digest' finished.", conversation_id="c7", group="Digest")
    assert channel.calls == [("notification", ("Scheduled task 'Digest' finished.", "c7", None, "Digest"))]


# --- auto-approval: the one frame with no silent degradation --------------------------------------


async def test_auto_approval_uses_the_frame_when_the_channel_has_one():
    """The conversation rides along, as it does on `alert`: the decision belongs to one turn, and a
    front end with a conversation list has to be able to file it against that turn rather than against
    whatever is on screen when the card arrives."""
    channel = RichChannelDouble()
    ui = ChannelUI(channel)
    await ui.show_auto_approval(
        "run_command", {"command": "ls"}, approved=True, reason="lists files", model="ollama:b", conversation_id="c1"
    )
    assert channel.calls[-1] == (
        "auto_approval",
        ("run_command", {"command": "ls"}, True, "lists files", "ollama:b", "c1", None),
    )


async def test_auto_approval_card_carries_the_agent():
    channel = RichChannelDouble()
    await ChannelUI(channel).show_auto_approval("run_command", {}, approved=True, reason="ok", model="m", agent="coder")
    assert channel.calls[-1][1][-1] == "coder"


async def test_plain_text_auto_approval_names_the_agent():
    channel = BareChannel()
    ui = ChannelUI(channel)
    await ui.show_auto_approval("run_command", {}, approved=True, reason="", model="m", agent="coder")
    await ui.show_auto_approval("run_command", {}, approved=False, reason="", model="m", agent="coder")
    assert channel.sent == [
        "[auto-approval] m auto-approved coder's run_command({})",
        "[auto-approval] m is asking you to approve coder's run_command({})",
    ]


async def test_a_channel_with_a_one_argument_approval_method_is_not_called_the_old_way():
    """The approval frame is a breaking change for such a channel: it always receives `agent`, so it
    fails loudly instead of being half-served by a branch that only some calls take."""

    class OldChannel(BareChannel):
        async def send_approval_request(self, name, arguments):
            raise AssertionError("must not be called without agent")

    with pytest.raises(TypeError):
        await ChannelUI(OldChannel()).ask_approval("run_command", {})


async def test_an_empty_reason_does_not_trail_a_colon():
    """The sentence a user reads about a call that was waved through, so it should read as a sentence.

    Both directions, because the fallback builds them separately.
    """
    channel = BareChannel()
    ui = ChannelUI(channel)
    await ui.show_auto_approval("run_command", {"command": "ls"}, approved=True, reason="", model="ollama:b")
    await ui.show_auto_approval("run_command", {"command": "ls"}, approved=False, reason="", model="ollama:b")
    assert [line.endswith("run_command({'command': 'ls'})") for line in channel.sent] == [True, True]


async def test_auto_approval_degrades_to_a_self_contained_line():
    channel = BareChannel()
    ui = ChannelUI(channel)
    await ui.show_auto_approval("run_command", {"command": "ls"}, approved=True, reason="lists files", model="ollama:b")
    line = channel.sent[0]
    # Self-contained, like `alert`'s fallback: a channel with no card surface has to carry the whole
    # decision in words, because there is nothing else for the user to click on.
    assert "auto-approved" in line
    assert "run_command" in line
    assert "lists files" in line
    assert "ollama:b" in line


async def test_an_escalation_says_it_is_going_to_the_user():
    channel = BareChannel()
    ui = ChannelUI(channel)
    await ui.show_auto_approval(
        "run_command", {"command": "ls"}, approved=False, reason="cannot be undone", model="ollama:b"
    )
    assert "asking you" in channel.sent[0]


async def test_show_artifact_uses_the_rich_frame_when_the_channel_has_one():
    rich = RichChannelDouble()
    record = {"url": "/images/a.png", "filename": "cat.png", "mime": "image/png", "after": 1}
    await ChannelUI(rich).show_artifact(record, Path("/data/images/a.png"))
    assert rich.calls == [("artifact", (record,))]
    assert rich.sent == []


async def test_show_artifact_degrades_to_a_line_naming_the_file_on_disk():
    bare = BareChannel()
    record = {"url": "/images/a.png", "filename": "cat.png", "mime": "image/png", "after": 1}
    await ChannelUI(bare).show_artifact(record, Path("/data/images/a.png"))
    assert bare.sent == ["Image saved: /data/images/a.png"]


async def test_a_request_is_a_no_op_and_only_a_wrap_up_or_error_ending_is_a_line_on_a_bare_channel():
    channel = BareChannel()
    ui = ChannelUI(channel)
    await ui.show_request({"run_id": "r", "parent_run_id": None, "round": 0})
    await ui.show_ending(
        {"agent": "assistant", "ended_by": "wrap_up", "round": 10, "max_iterations": 10, "error": None}
    )
    await ui.show_ending(
        {"agent": "researcher", "ended_by": "error", "round": 2, "max_iterations": None, "error": "TruncatedTurnError"}
    )
    await ui.show_ending(
        {"agent": "assistant", "ended_by": "answered", "round": 1, "max_iterations": 10, "error": None}
    )

    assert channel.sent == [
        "[assistant] ended: tool-use limit of 10 rounds reached; answered on a forced wrap-up call",
        "[researcher] ended: error (TruncatedTurnError) at round 3",
    ]


def test_ending_lines_count_rounds_from_one_and_need_no_counter_for_a_wrap_up():
    from kokua.channels.ui import ending_line

    base = {"agent": "a", "round": 0, "max_iterations": 10, "error": None}
    assert ending_line({**base, "ended_by": "answered"}) == "[a] ended: answered at round 1 of 10"
    assert ending_line({**base, "ended_by": "answered", "max_iterations": None}) == "[a] ended: answered at round 1"
    wrap = {**base, "ended_by": "wrap_up", "round": 10}
    assert ending_line(wrap) == "[a] ended: tool-use limit of 10 rounds reached; answered on a forced wrap-up call"
    assert ending_line({**wrap, "max_iterations": None}) == (
        "[a] ended: tool-use limit reached; answered on a forced wrap-up call"
    )
    error = {**base, "ended_by": "error", "error": "boom", "round": 2}
    assert ending_line(error) == "[a] ended: error (boom) at round 3 of 10"
    assert ending_line({**error, "error": None}) == "[a] ended: error at round 3 of 10"
    assert ending_line({**base, "ended_by": None}) == "[a] ended at round 1 of 10"


async def test_show_turn_is_a_no_op_on_a_bare_channel():
    channel = BareChannel()
    await ChannelUI(channel).show_turn(3)
    assert channel.sent == []


# --- toolset labels -------------------------------------------------------------------------------


class _DrainingChannel(BareChannel):
    """Drains a streamed reply into ``chunks``, the way a real channel's send loop consumes one."""

    def __init__(self):
        super().__init__()
        self.chunks: list[StreamChunk] = []

    async def send(self, content, *, reply_to=None) -> None:
        if isinstance(content, str):
            self.sent.append(content)
            return
        self.chunks.extend([chunk async for chunk in content])


def _tool_chunk(name: str) -> StreamChunk:
    return StreamChunk(StreamingContentType.TOOL_CALLING, {"name": name, "arguments": {}, "response": "ok"})


async def test_a_streamed_tool_call_carries_the_toolset_it_came_from():
    channel = _DrainingChannel()
    ui = ChannelUI(channel, toolset_of={"get_web_content": "web"}.get)
    thinking = StreamChunk(StreamingContentType.THINKING, "hmm")
    await ui.send(_chunks(thinking, _tool_chunk("get_web_content")))
    assert channel.chunks[0] == thinking
    assert channel.chunks[1].content == {
        "name": "get_web_content",
        "arguments": {},
        "response": "ok",
        "toolset": "web",
    }


async def test_a_tool_call_with_no_known_toolset_streams_without_the_key():
    """Absent rather than None, so a renderer tells "unknown" from a toolset by the key alone."""
    channel = _DrainingChannel()
    await ChannelUI(channel, toolset_of=lambda tool: None).send(_chunks(_tool_chunk("mystery")))
    assert "toolset" not in channel.chunks[0].content


async def test_stream_activity_labels_tool_calls_too():
    """A planned turn's executor streams through stream_activity rather than send, and is labelled alike."""
    seen: list[StreamChunk] = []

    class _Streaming(BareChannel):
        async def stream_activity(self, chunks, *, show_answer=False) -> str:
            seen.extend([chunk async for chunk in chunks])
            return ""

    ui = ChannelUI(_Streaming(), toolset_of={"calculate": "compute"}.get)
    await ui.stream_activity(_chunks(_tool_chunk("calculate")))
    assert seen[0].content["toolset"] == "compute"
