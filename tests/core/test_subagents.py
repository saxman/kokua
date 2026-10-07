"""The sub-agent reporter: AIMU spawn callbacks become display frames plus recorded events."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from aimu.models import StreamChunk, StreamingContentType

from kokua import payloads
from kokua.channels.ui import ChannelUI
from kokua.core.messaging import MessageBus, current_address, current_bus
from kokua.core.subagents import RESPONSE_PREVIEW_CHARS, SubagentReporter, subagent_events
from kokua.toolsets.messaging import send_message
from tests.channels import SubagentCapturingChannel

# No existing reporter test produces a response over RESPONSE_PREVIEW_CHARS, so nothing is ever
# written here. Deliberately a path that does not exist: a test that starts spilling without passing
# its own tmp_path should fail loudly rather than quietly writing into the system temp directory.
_UNUSED_PAYLOADS_PATH = Path(tempfile.gettempdir()) / "kokua-test-payloads-never-written"


def _reporter(model_for=lambda agent_type: None, thinking_for=lambda agent_type: None, payloads_path=None):
    channel = SubagentCapturingChannel()
    reporter = SubagentReporter(
        ChannelUI(channel),
        model_for=model_for,
        thinking_for=thinking_for,
        payloads_path=payloads_path or _UNUSED_PAYLOADS_PATH,
    )
    return reporter, channel


def _collect():
    """Install a per-turn collector and return the list the reporter appends to."""
    events: list[dict] = []
    subagent_events.set(events)
    return events


def _thinking(text):
    return StreamChunk(StreamingContentType.THINKING, text)


def _tool_call(name, arguments, response=None):
    return StreamChunk(StreamingContentType.TOOL_CALLING, {"name": name, "arguments": arguments, "response": response})


def _generating(text):
    return StreamChunk(StreamingContentType.GENERATING, text)


def _continuing(kind, prompt):
    return StreamChunk(StreamingContentType.CONTINUING, {"kind": kind, "prompt": prompt})


def _inbox(text):
    return StreamChunk(StreamingContentType.INBOX, {"text": text})


async def test_a_spawn_opens_a_running_card_and_closes_it_with_the_answer():
    """A provider that yields no GENERATING chunk streams nothing, so the terminal event carries the
    text rather than leaving the card empty."""
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("researcher-abc", "researcher", "find X")
    await reporter.finished("researcher-abc", "the answer", None)
    assert channel.subagent_frames == [
        {"id": "researcher-abc", "role": "researcher", "task": "find X", "status": "running"},
        {"id": "researcher-abc", "status": "done", "append": {"kind": "answer", "text": "the answer"}},
    ]


async def test_generic_spawn_without_a_role_still_names_the_card():
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("subagent-abc", None, "find X")
    assert channel.subagent_frames[0]["role"] == "subagent"


async def test_nested_reasoning_reaches_the_card():
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking("hmm"))
    assert channel.subagent_frames[-1] == {"id": "r-1", "append": {"kind": "reasoning", "text": "hmm"}}


async def test_nested_reasoning_with_no_text_appends_nothing():
    """An empty THINKING chunk would open a reasoning block with nothing in it."""
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking(""))
    assert len(channel.subagent_frames) == 1


async def test_nested_tool_calls_reach_the_card():
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _tool_call("get_web_content", {"url": "https://example.com"}))
    assert channel.subagent_frames[-1] == {
        "id": "r-1",
        "append": {
            "kind": "tool",
            "name": "get_web_content",
            "arguments": {"url": "https://example.com"},
            "response": None,
        },
    }


async def test_a_nested_tool_entry_carries_what_the_call_returned():
    """A sub-agent's card is the only place its nested calls are ever shown, so dropping the result
    would leave no way to see what the spawn actually retrieved."""
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _tool_call("get_web_content", {"url": "https://example.com"}, "<html>X</html>"))
    assert channel.subagent_frames[-1]["append"]["response"] == "<html>X</html>"


async def test_oversized_tool_response_is_recorded_as_preview_and_reference(tmp_path):
    """A megabyte of tool output does not go into session metadata.

    One PDF fetched as text put 7.8 MB into a single recorded card, and 51.9 MB of a 56.8 MB session
    file was this one field.
    """
    payloads_path = tmp_path / "payloads"
    reporter, _channel = _reporter(payloads_path=payloads_path)
    events = _collect()
    response = "x" * (RESPONSE_PREVIEW_CHARS * 3)

    await reporter.spawned("researcher-abc", "researcher", "read the PDF")
    await reporter.chunk("researcher-abc", _tool_call("get_web_content", {"url": "u"}, response))

    append = events[-1]["append"]
    assert append["kind"] == "tool"
    assert append["name"] == "get_web_content"
    assert append["arguments"] == {"url": "u"}
    assert append["response"] == response[:RESPONSE_PREVIEW_CHARS]
    assert append["response_bytes"] == len(response)
    assert payloads.read_text(payloads_path, append["response_ref"]) == response


async def test_small_tool_response_is_recorded_inline(tmp_path):
    """Below the threshold nothing changes, so an ordinary card is untouched and nothing is written."""
    payloads_path = tmp_path / "payloads"
    reporter, _channel = _reporter(payloads_path=payloads_path)
    events = _collect()

    await reporter.spawned("researcher-abc", "researcher", "look it up")
    await reporter.chunk("researcher-abc", _tool_call("search", {"q": "kauai"}, "a short answer"))

    append = events[-1]["append"]
    assert append["response"] == "a short answer"
    assert "response_ref" not in append
    assert "response_bytes" not in append
    assert not payloads_path.exists()


async def test_live_frame_and_recorded_event_are_identical(tmp_path):
    """A card must not change when the user switches away and back.

    The frame sent live and the entry replayed from metadata come from one dict, so capping at the
    recording point rather than in send_history is what keeps them the same.
    """
    reporter, channel = _reporter(payloads_path=tmp_path / "payloads")
    events = _collect()
    response = "y" * (RESPONSE_PREVIEW_CHARS * 2)

    await reporter.spawned("researcher-abc", "researcher", "read the PDF")
    await reporter.chunk("researcher-abc", _tool_call("get_web_content", {"url": "u"}, response))

    assert channel.subagent_frames[-1] == events[-1]


async def test_oversized_response_with_a_lone_surrogate_falls_back_to_inline(tmp_path):
    """A tool result decoded upstream with errors="surrogateescape" (a binary file fetched as text is
    the likely source) can carry lone surrogates that strict UTF-8 cannot encode, so save_text raises.
    Recording runs on a live turn's path, so the fallback is to keep the response inline rather than
    let the exception end the turn; the card is oversized but the turn is not lost.
    """
    payloads_path = tmp_path / "payloads"
    reporter, _channel = _reporter(payloads_path=payloads_path)
    events = _collect()
    response = ("x" * (RESPONSE_PREVIEW_CHARS * 3)) + "\udcff"

    await reporter.spawned("researcher-abc", "researcher", "read the file")
    await reporter.chunk("researcher-abc", _tool_call("get_web_content", {"url": "u"}, response))

    append = events[-1]["append"]
    assert append["response"] == response
    assert "response_ref" not in append
    assert "response_bytes" not in append
    assert not payloads_path.exists()


async def test_a_response_of_exactly_the_threshold_stays_inline(tmp_path):
    """The boundary itself, not just something safely past it. ``<=`` is what makes a response of
    exactly RESPONSE_PREVIEW_CHARS keep today's shape; a suite that only ever tries sizes well past the
    cap would stay green if that were quietly changed to ``<``, and every response of exactly the cap
    would start spilling with nothing to catch it."""
    payloads_path = tmp_path / "payloads"
    reporter, _channel = _reporter(payloads_path=payloads_path)
    events = _collect()
    response = "x" * RESPONSE_PREVIEW_CHARS

    await reporter.spawned("researcher-abc", "researcher", "look it up")
    await reporter.chunk("researcher-abc", _tool_call("search", {"q": "kauai"}, response))

    append = events[-1]["append"]
    assert append["response"] == response
    assert "response_ref" not in append
    assert "response_bytes" not in append
    assert not payloads_path.exists()


async def test_one_character_past_the_threshold_spills(tmp_path):
    """The other side of the same boundary: one character more than the threshold is enough to spill,
    pinning the cap from both directions."""
    payloads_path = tmp_path / "payloads"
    reporter, _channel = _reporter(payloads_path=payloads_path)
    events = _collect()
    response = "x" * (RESPONSE_PREVIEW_CHARS + 1)

    await reporter.spawned("researcher-abc", "researcher", "look it up")
    await reporter.chunk("researcher-abc", _tool_call("search", {"q": "kauai"}, response))

    append = events[-1]["append"]
    assert append["response"] == response[:RESPONSE_PREVIEW_CHARS]
    assert append["response_bytes"] == len(response)
    assert payloads.read_text(payloads_path, append["response_ref"]) == response


async def test_oversized_response_falls_back_to_inline_when_the_disk_write_fails(tmp_path, monkeypatch):
    """Before this cap, the TOOL_CALLING branch never touched disk, so a live turn never depended on a
    write succeeding. It does now, and a full disk, a permissions problem, or a payloads directory that
    cannot be created are exactly the conditions under which a user least wants their turn to die, so
    an OSError out of save_text gets the same inline fallback as the surrogate case."""
    payloads_path = tmp_path / "payloads"
    reporter, _channel = _reporter(payloads_path=payloads_path)
    events = _collect()
    response = "x" * (RESPONSE_PREVIEW_CHARS * 3)

    def _raise(_payloads_path, _text):
        raise OSError("disk full")

    monkeypatch.setattr(payloads, "save_text", _raise)

    await reporter.spawned("researcher-abc", "researcher", "read the file")
    await reporter.chunk("researcher-abc", _tool_call("get_web_content", {"url": "u"}, response))

    append = events[-1]["append"]
    assert append["response"] == response
    assert "response_ref" not in append
    assert "response_bytes" not in append


async def test_generated_text_streams_chunk_by_chunk():
    """The card's text arrives live, like the parent's own answer."""
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("half "))
    await reporter.chunk("r-1", _generating("an answer"))
    assert channel.subagent_frames[1:] == [
        {"id": "r-1", "append": {"kind": "answer", "text": "half "}},
        {"id": "r-1", "append": {"kind": "answer", "text": "an answer"}},
    ]


async def test_a_streamed_answer_is_not_repeated_by_the_finish_frame():
    """The text is already on screen, so repeating it on completion would show the answer twice."""
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("the answer"))
    await reporter.finished("r-1", "the answer", None)
    assert channel.subagent_frames[-1] == {"id": "r-1", "status": "done"}
    assert events[-1] == {"id": "r-1", "status": "done"}


async def test_answer_chunks_coalesce_when_recorded_but_arrive_as_separate_frames():
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("one "))
    await reporter.chunk("r-1", _generating("two"))
    await reporter.finished("r-1", "one two", None)
    assert [f.get("append", {}).get("text") for f in channel.subagent_frames[1:]] == ["one ", "two", None]
    assert events == [
        {"id": "r-1", "role": "researcher", "task": "find X", "status": "running"},
        {"id": "r-1", "append": {"kind": "answer", "text": "one two"}},
        {"id": "r-1", "status": "done"},
    ]


async def test_a_tool_call_between_two_generations_starts_a_second_answer_entry():
    """One answer entry per round, so a multi-round spawn reads as rounds rather than one run-on
    block. The parent's own rounds are separated the same way, by the tool call sitting between two
    generations. Neither level draws a loop marker for an ordinary round; that marker is reserved for
    the round the loop injected, which is what the two tests below cover."""
    reporter, _channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("first round"))
    await reporter.chunk("r-1", _tool_call("get_web_content", {"url": "u"}))
    await reporter.chunk("r-1", _generating("second round"))
    assert [e.get("append", {}).get("text") for e in events[1:]] == ["first round", None, "second round"]


async def test_an_injected_round_reaches_the_card_with_what_the_worker_was_told():
    """A worker that hits the round cap is told to stop calling tools and answer from what it has.
    Without this, the card showed a worker going quiet and coming back thinner, with no reason given."""
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("researcher-abc", "researcher", "find X")
    await reporter.chunk("researcher-abc", _continuing("final_answer", "You have reached the tool-use limit."))

    entry = {"kind": "loop", "reason": "final_answer", "text": "You have reached the tool-use limit."}
    assert channel.subagent_frames[-1] == {"id": "researcher-abc", "append": entry}
    assert events[-1] == {"id": "researcher-abc", "append": entry}


async def test_a_workers_inbox_chunk_is_recorded_on_its_card():
    """A message sent into a worker already running is the user's own words, not the loop injecting a
    round of its own, so it has to land as its own kind rather than a `loop` entry (which would credit
    the loop with what a person said)."""
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _inbox("stop that"))

    entry = {"kind": "message", "text": "stop that"}
    assert channel.subagent_frames[-1] == {"id": "r-1", "append": entry}
    assert events[-1] == {"id": "r-1", "append": entry}


async def test_a_message_chunk_becomes_a_card_entry_naming_its_sender():
    """The card entry carries whatever a real bus drain produced, not a hand-written stand-in for it.

    A fixture that types the attributed text itself (``{"text": "researcher#1 said: use the index"}``)
    would pass this test whether or not ``core/messaging.py`` ever composed that prefix at all, since
    ``SubagentReporter.chunk`` only ever repeats the text it is given. So the input here is the real
    output of :meth:`MessageBus.entry_reader`'s drain -- ``_for_model``'s own ``[message from {sender}]``
    rendering of a worker's note to its parent -- and the assertion compares the card entry against
    that same variable rather than against a second, independently typed copy of it. Whether that
    rendering happens at all, which is the security property, is pinned separately in
    ``tests/core/test_messaging.py``; what this test owns is narrower: the card shows the sender's
    words unmodified, carrying whatever attribution the drain actually put there.
    """
    bus = MessageBus()
    bus.send("use the index", sender="researcher#1", to="assistant")
    delivered = bus.entry_reader("assistant")()
    assert len(delivered) == 1  # one message sent, one line drained; see the drain call just above

    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _inbox(delivered[0]))

    entry = {"kind": "message", "text": delivered[0]}
    assert channel.subagent_frames[-1] == {"id": "r-1", "append": entry}
    assert events[-1] == {"id": "r-1", "append": entry}


async def test_an_injected_round_starts_a_second_answer_entry():
    """The break is the point. A nudge fires on an empty turn, so nothing else sits between the two
    generations to close the block, and the card would otherwise show one uninterrupted answer."""
    reporter, _ = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("first round"))
    await reporter.chunk("r-1", _continuing("continuation", "Keep going."))
    await reporter.chunk("r-1", _generating("second round"))

    answers = [e["append"]["text"] for e in events if e.get("append", {}).get("kind") == "answer"]
    assert answers == ["first round", "second round"]


async def test_a_tool_round_adds_no_loop_entry_to_the_card():
    """AIMU raises the iteration counter for a tool round too, and injects nothing there. The tool
    entry is what separates those rounds, as it always has."""
    reporter, _ = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _tool_call("get_web_content", {"url": "u"}, "page"))

    assert [e.get("append", {}).get("kind") for e in events[1:]] == ["tool"]


async def test_an_interleaved_spawn_breaks_the_answer_block():
    reporter, _channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "a")
    await reporter.spawned("r-2", "researcher", "b")
    await reporter.chunk("r-1", _generating("one"))
    await reporter.chunk("r-2", _generating("two"))
    await reporter.chunk("r-1", _generating("three"))
    assert [(e["id"], e["append"]["text"]) for e in events if "append" in e] == [
        ("r-1", "one"),
        ("r-2", "two"),
        ("r-1", "three"),
    ]


async def test_the_streamed_answer_marker_is_released_when_the_spawn_finishes():
    """The reporter lives as long as the connection, so tracking which spawns streamed must not grow
    an entry per spawn ever made."""
    reporter, _channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _generating("the answer"))
    await reporter.finished("r-1", "the answer", None)
    assert reporter._streamed_answers == set()


async def test_a_stopped_spawn_keeps_what_it_streamed():
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "writer", "draft it")
    await reporter.chunk("r-1", _generating("partial dra"))
    await reporter.finished("r-1", "partial dra", asyncio.CancelledError())
    assert channel.subagent_frames[1:] == [
        {"id": "r-1", "append": {"kind": "answer", "text": "partial dra"}},
        {"id": "r-1", "status": "stopped"},
    ]


async def test_a_failed_spawn_ends_the_card_in_error():
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.finished("r-1", "", ValueError("child exploded"))
    assert channel.subagent_frames[-1] == {
        "id": "r-1",
        "status": "error",
        "append": {"kind": "error", "text": "child exploded"},
    }
    assert events[-1]["status"] == "error"


async def test_a_cancelled_spawn_records_before_it_tries_to_send():
    """The reporter runs inside the cancelled task, so recording must not depend on the send."""

    class _RefusingChannel(SubagentCapturingChannel):
        async def send_subagent(self, event):
            raise asyncio.CancelledError

    reporter = SubagentReporter(
        ChannelUI(_RefusingChannel()),
        model_for=lambda agent_type: None,
        thinking_for=lambda agent_type: None,
        payloads_path=_UNUSED_PAYLOADS_PATH,
    )
    events = _collect()
    await reporter.finished("r-1", "partial", asyncio.CancelledError())
    assert events == [{"id": "r-1", "status": "stopped", "append": {"kind": "answer", "text": "partial"}}]


async def test_events_are_recorded_for_replay_and_reasoning_is_coalesced():
    reporter, _channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking("one "))
    await reporter.chunk("r-1", _thinking("two"))
    await reporter.finished("r-1", "the answer", None)
    assert events == [
        {"id": "r-1", "role": "researcher", "task": "find X", "status": "running"},
        {"id": "r-1", "append": {"kind": "reasoning", "text": "one two"}},
        {"id": "r-1", "status": "done", "append": {"kind": "answer", "text": "the answer"}},
    ]


async def test_coalescing_never_merges_across_two_concurrent_spawns():
    reporter, _channel = _reporter()
    events = _collect()
    await reporter.spawned("r-1", "researcher", "a")
    await reporter.spawned("r-2", "researcher", "b")
    await reporter.chunk("r-1", _thinking("one"))
    await reporter.chunk("r-2", _thinking("two"))
    await reporter.chunk("r-1", _thinking("three"))
    reasoning = [e["append"]["text"] for e in events if "append" in e]
    assert reasoning == ["one", "two", "three"]


async def test_coalescing_survives_interleaving_with_another_turns_activity():
    """One reporter serves every conversation's turns, and those run concurrently by default. A
    reporter-level 'last reasoning' slot (the old design) would let one turn's events clobber another
    turn's coalescing state; this drives two turns as real overlapping tasks, each with its own
    subagent_events list, and asserts each still coalesces its own consecutive chunks into one entry,
    in order, despite the interleaving."""
    reporter, _channel = _reporter()

    async def run_turn(spawn_id, chunks):
        events: list[dict] = []
        subagent_events.set(events)
        await reporter.spawned(spawn_id, "researcher", "find X")
        for text in chunks:
            await reporter.chunk(spawn_id, _thinking(text))
            await asyncio.sleep(0)  # yield, so the two turns' record() calls genuinely interleave
        await reporter.finished(spawn_id, "done", None)
        return events

    events_a, events_b = await asyncio.gather(
        run_turn("r-1", ["one ", "two ", "three"]),
        run_turn("r-2", ["uno ", "dos"]),
    )

    def reasoning_of(events):
        return [e["append"]["text"] for e in events if e.get("append", {}).get("kind") == "reasoning"]

    assert reasoning_of(events_a) == ["one two three"]
    assert reasoning_of(events_b) == ["uno dos"]


async def test_recording_is_a_copy_so_coalescing_cannot_mutate_a_sent_frame():
    reporter, channel = _reporter()
    _collect()
    await reporter.spawned("r-1", "researcher", "find X")
    await reporter.chunk("r-1", _thinking("one "))
    await reporter.chunk("r-1", _thinking("two"))
    assert [f["append"]["text"] for f in channel.subagent_frames[1:]] == ["one ", "two"]


async def test_no_collector_installed_still_displays():
    """A spawn outside any turn (there is no such path today) must not raise."""
    reporter, channel = _reporter()
    subagent_events.set(None)
    await reporter.spawned("r-1", "researcher", "find X")
    assert len(channel.subagent_frames) == 1


async def test_a_spawn_records_the_model_that_produced_its_output():
    """A conversation's stored JSON has to answer which model produced a worker's answer, and the
    workers of one turn need not share one: each runs on its own [agents.*].model or the default."""
    reporter, channel = _reporter(model_for=lambda agent_type: f"ollama:{agent_type}-model")
    events = _collect()
    await reporter.spawned("researcher-abc", "researcher", "find X")
    assert events[0]["model"] == "ollama:researcher-model"
    assert channel.subagent_frames[0]["model"] == "ollama:researcher-model"


async def test_a_spawn_with_no_model_configured_anywhere_records_no_model():
    """AIMU resolves an unset model when the client is built, so there is no string to record here.
    Omitted rather than null: a key present means it is the answer."""
    reporter, _ = _reporter()
    events = _collect()
    await reporter.spawned("researcher-abc", "researcher", "find X")
    assert "model" not in events[0]


async def test_a_spawn_records_the_thinking_its_worker_ran_at():
    """A worker need not reason at the effort that answered the turn, so the card is the only record."""
    reporter, channel = _reporter(thinking_for=lambda agent_type: "high")
    events = _collect()
    await reporter.spawned("s-1", "researcher", "find sources")
    assert events[0]["thinking"] == "high"
    assert channel.subagent_frames[0]["thinking"] == "high"


async def test_a_spawn_with_reasoning_off_records_that_rather_than_omitting_it():
    """``False`` is a declaration, so the card guard cannot be a truthiness test."""
    reporter, channel = _reporter(thinking_for=lambda agent_type: False)
    events = _collect()
    await reporter.spawned("s-1", "formatter", "reformat this")
    assert events[0]["thinking"] is False


async def test_a_spawn_with_no_thinking_configured_anywhere_records_none():
    reporter, channel = _reporter()
    events = _collect()
    await reporter.spawned("s-1", "researcher", "find sources")
    assert "thinking" not in events[0]


# The other half of refusing a run with no address of its own. `send_message` refuses when
# `current_address` is unset,
# and these cover why it is ever *correctly* unset or restored rather than merely hoping `reader()`
# ran. `core/messaging.py` itself cannot do this: a reader opens once, at a run's own start, and
# nothing in AIMU's Inbox protocol calls back when a run ends, so there is no hook there to restore
# the caller's address once a spawned run returns. `spawned`/`finished` are the one pair AIMU calls
# around every spawn Kokua makes, declared or composed (both factories pass this reporter as
# `observer`), which is what makes them the right place for this instead.


async def test_spawned_clears_current_address_for_the_run_about_to_start():
    reporter, _ = _reporter()
    current_address.set("assistant")

    await reporter.spawned("s-1", "researcher", "find sources")

    assert current_address.get() is None


async def test_finished_restores_whatever_spawned_saved():
    reporter, _ = _reporter()
    current_address.set("assistant")

    await reporter.spawned("s-1", "researcher", "find sources")
    # Standing in for the spawned run opening its own reader mid-flight, between `spawned` and
    # `finished`, the way a real one does inside `agent.run()`.
    current_address.set("researcher#1")
    await reporter.finished("s-1", "the answer", None)

    assert current_address.get() == "assistant"


async def test_finished_restores_even_when_the_spawn_failed():
    # `_run_observed` calls `finished` from a `finally`, so the restore must not depend on a clean
    # result either. Nothing here reaches into AIMU's loop to prove that ordering; it only pins that
    # *this* reporter's own restore does not skip itself when `error` is set.
    reporter, _ = _reporter()
    current_address.set("assistant")

    await reporter.spawned("s-1", "researcher", "find sources")
    current_address.set("researcher#1")
    await reporter.finished("s-1", "", ValueError("boom"))

    assert current_address.get() == "assistant"


async def test_nested_spawns_restore_in_the_right_order():
    # A composed worker that itself composes one more (depth > 1) is two `spawned`/`finished` pairs,
    # not one, and they nest: the outer's `finished` must not run before the inner's, and each must
    # restore to what was current when *it* started, not to some shared default. Tokens keyed by
    # spawn_id (see `SubagentReporter.__init__`) are what keeps this correct without an explicit
    # stack: each `finished` only ever resets the token its own `spawned` saved.
    reporter, _ = _reporter()
    current_address.set("assistant")

    await reporter.spawned("outer", "composed", "task")
    current_address.set("researcher#1")  # the outer spawn claims its own address

    await reporter.spawned("inner", "composed", "nested task")
    current_address.set("coder#1")  # the inner spawn claims its own, deeper still
    assert current_address.get() == "coder#1"

    await reporter.finished("inner", "inner answer", None)
    assert current_address.get() == "researcher#1"  # back to the outer spawn's own address

    await reporter.finished("outer", "outer answer", None)
    assert current_address.get() == "assistant"  # back to the original caller


async def test_a_real_spawn_through_the_reporter_leaves_the_parents_own_send_correctly_attributed():
    """The join the bracket exists for, which neither half proves alone.

    Each half is pinned on its own
    elsewhere: `spawned`/`finished` restoring `current_address` is the tests above, and `bus.send`
    attributing a message to whatever `current_address` holds is `tests/core/test_messaging.py`.
    Neither is the join the bracket was built for -- `send_message`, called by the parent right after
    a real spawn runs through this reporter, attributed to the parent and not to the worker it just
    spawned -- which is the shape that has to be reconstructed by hand whenever this is questioned, so
    it is pinned here instead.
    """
    reporter, _ = _reporter()
    bus = MessageBus()
    bus.entry_reader("assistant")

    bus_token = current_bus.set(bus)
    try:
        await reporter.spawned("s-1", "researcher", "find sources")
        # Stands in for the spawned worker's own `agent.run()` calling `_open_inbox`, which is what a
        # real spawn does between `spawned` and `finished` -- see `spawned`'s own docstring.
        bus.reader("subagent-researcher")
        await reporter.finished("s-1", "the answer", None)

        # The spawn has returned; this is the parent's own next tool call in the same round. Nothing
        # between `finished` and this line reopens the parent's reader, so if the bracket's restore
        # had not run, this would still be attributed to "researcher#1".
        receipt = send_message("researcher#1", "any luck?")
    finally:
        current_bus.reset(bus_token)

    assert "no agent" not in receipt.lower()
    assert "no address" not in receipt.lower()
    resubmit, report = bus.close()
    assert resubmit == []
    assert len(report) == 1
    assert report[0].sender == "assistant"
    assert report[0].to == "researcher#1"


# Which spawn made which. AIMU's callbacks name a spawn but not its spawner, so `current_spawn` is
# bracketed by `spawned`/`finished` the way `current_address` is, and each create event names the
# spawn current when it opened.


async def test_a_spawn_at_the_turns_own_level_names_no_parent():
    reporter, channel = _reporter()

    await reporter.spawned("s-1", "researcher", "find sources")

    assert "parent" not in channel.subagent_frames[0]


async def test_a_spawn_made_inside_another_names_it_as_parent():
    reporter, channel = _reporter()

    await reporter.spawned("outer", "composed", "task")
    await reporter.spawned("inner", "composed", "nested task")

    assert channel.subagent_frames[1]["parent"] == "outer"


async def test_a_finished_spawn_stops_being_the_parent_of_what_follows():
    reporter, channel = _reporter()

    await reporter.spawned("outer", "composed", "task")
    await reporter.spawned("first", "composed", "nested task")
    await reporter.finished("first", "done", None)
    await reporter.spawned("second", "composed", "nested task")
    await reporter.finished("second", "done", None)
    await reporter.finished("outer", "done", None)
    await reporter.spawned("later", "composed", "next task")

    creates = {frame["id"]: frame for frame in channel.subagent_frames if "task" in frame}
    assert creates["second"]["parent"] == "outer", "a sibling must not be recorded as the first one's child"
    assert "parent" not in creates["later"]


async def test_a_failed_spawn_stops_being_a_parent_too():
    reporter, channel = _reporter()

    await reporter.spawned("outer", "composed", "task")
    await reporter.finished("outer", "", ValueError("boom"))
    await reporter.spawned("next", "composed", "task")

    assert "parent" not in channel.subagent_frames[-1]


async def test_the_parent_is_recorded_for_replay():
    reporter, _ = _reporter()
    events = _collect()

    await reporter.spawned("outer", "composed", "task")
    await reporter.spawned("inner", "composed", "nested task")

    assert events[1]["parent"] == "outer"


class _ScriptedAgent:
    """Stands in for the agent AIMU's ``_run_observed`` drives: ``await agent.run(task, stream=True)``
    returning an async iterator. ``during`` runs between two chunks, standing in for the tool round in
    which a worker spawns workers of its own."""

    def __init__(self, during=None):
        self._during = during

    async def run(self, task, stream=True):
        async def chunks():
            yield _thinking("planning")
            if self._during is not None:
                await self._during()
            yield _generating("answer")

        return chunks()


async def test_nested_and_concurrent_spawns_through_aimus_own_spawn_path_name_the_right_parent():
    """The same claims as above, through AIMU's real ``_run_observed`` rather than hand-ordered calls,
    because whether a context variable set in ``spawned`` is visible to a spawn made inside the child's
    run depends on how AIMU iterates that run, which a hand-written sequence cannot establish. The two
    grandchildren run concurrently, as a round of two tool calls does, so each gets a copied Context."""
    from aimu.aio.tools.builtin import _run_observed

    reporter, _ = _reporter()
    events = _collect()

    async def two_concurrent_children():
        await asyncio.gather(
            _run_observed(_ScriptedAgent(), "left", "a", reporter),
            _run_observed(_ScriptedAgent(), "right", "b", reporter),
        )

    await _run_observed(_ScriptedAgent(during=two_concurrent_children), "outer", "task", reporter)
    await _run_observed(_ScriptedAgent(), "after", "task", reporter)

    creates = {event["role"]: event for event in events if "task" in event}
    outer_id = creates["outer"]["id"]
    assert "parent" not in creates["outer"]
    assert creates["left"]["parent"] == outer_id
    assert creates["right"]["parent"] == outer_id
    assert "parent" not in creates["after"], "a finished spawn must not be the parent of the next one"
