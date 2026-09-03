"""The ChannelUI degradation matrix.

This is the contract that lets the core stop asking ``getattr(channel, "send_x", None)`` at seventeen
call sites: every optional frame has exactly one documented fallback, asserted here for a bare
``Channel`` and for a fully rich one.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from kokua.channels.ui import ChannelUI


class BareChannel:
    """The minimum a transport must implement: AIMU's base Channel contract."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content, *, reply_to=None) -> None:
        self.sent.append(content)

    def receive(self):
        raise NotImplementedError


class RichChannelDouble(BareChannel):
    """Every optional frame, recorded rather than rendered."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, tuple]] = []
        self.active_conversation_id: Optional[str] = None

    async def send_conversations(self, items: list[dict]) -> None:
        self.calls.append(("conversations", (items,)))

    async def send_notification(self, text: str) -> None:
        self.calls.append(("notification", (text,)))

    async def send_approval_request(self, name: str, arguments: Any) -> None:
        self.calls.append(("approval", (name, arguments)))

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
    await ui.ask_approval("execute_python", {"code": "1"})
    await ui.ask_plan_review("step 1", ["too vague"])
    await ui.show_plan("step 1")
    await ui.show_phase("Planner", "drafting")
    await ui.show_subagent({"role": "Plan reviewer"})
    await ui.finish_stream()
    assert await ui.stream_activity(_chunks("a", "b"), show_answer=True) == "ab"

    assert [name for name, _ in channel.calls] == [
        "conversations",
        "notification",
        "approval",
        "plan_review",
        "plan",
        "phase",
        "subagent",
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
