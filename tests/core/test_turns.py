"""TurnRunner: reactive turns, proactive (scheduled) turns, cancellation, and concurrency."""

from __future__ import annotations

import asyncio

import pytest

from aimu import PROVENANCE_KEY, PROVENANCE_PROACTIVE
from aimu.aio.channels.base import Channel, ChannelMessage

from kokua.core.assistant import Assistant
from kokua.toolsets.planning import PLANNING_WORKFLOW
from kokua.workflows import Workflow, WorkflowResult
from tests.channels import FakeChannel, _ConvCapturingChannel, _config, example_agents, planning_settings
from tests.fakes import _BlockingStreamClient, _RequestsToolOnce, _SeedsSystemMessage
from tests.helpers import MockAsyncModelClient


async def test_assistant_handles_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Sure, done."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(ChannelMessage(text="do a thing", channel="fake"), conversation_id=assistant._active_id)

    assert channel.sent == ["Sure, done."]
    assert assistant.history  # persisted at least the turn


async def test_assistant_proactive_message(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Don't forget lunch."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._proactive("remind")

    assert channel.sent == ["Don't forget lunch."]


async def test_assistant_proactive_tags_turn_provenance(tmp_path):
    from aimu.models import PROVENANCE_KEY, PROVENANCE_PROACTIVE

    channel = FakeChannel()
    client = MockAsyncModelClient(["Time for a walk."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._proactive("remind")

    tagged = [m.get(PROVENANCE_KEY) for m in assistant._agent.model_client.messages]
    assert PROVENANCE_PROACTIVE in tagged
    assert all(p in (None, PROVENANCE_PROACTIVE) for p in tagged)


async def test_assistant_proactive_keeps_the_loops_own_provenance(tmp_path):
    """The proactive tag must not overwrite the tags the agent loop already set on the turns it
    injected. A degenerate turn makes the loop inject a continuation nudge and mark it
    ``continuation``; stamping every message ``proactive`` on top left the nudge indistinguishable
    from user input, so the web transcript replayed it as a user bubble."""
    from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_KEY, PROVENANCE_PROACTIVE

    # An empty first response is a degenerate turn, which is what makes the loop nudge and retry.
    client = MockAsyncModelClient(["", "Here is the summary."])
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)

    await assistant._proactive("summarize the news")

    messages = assistant._agent.model_client.messages
    nudges = [m for m in messages if m.get(PROVENANCE_KEY) == PROVENANCE_CONTINUATION]
    assert len(nudges) == 1
    assert nudges[0]["role"] == "user"
    # Everything the loop did not tag itself is still marked as the unattended turn it belongs to.
    assert messages[-1].get(PROVENANCE_KEY) == PROVENANCE_PROACTIVE


async def test_proactive_reports_a_turn_the_model_had_no_room_to_finish(tmp_path):
    """A scheduled task whose conversation has outgrown the model's context window used to show a run
    of continuation prompts and no work: the agent loop read each cut-off turn as a model that failed
    to answer and nudged it, which only left less room. AIMU raises instead, and the task's failure
    report is where the user finds out, so it has to carry the reason and not just a class name."""
    channel = FakeChannel()
    client = MockAsyncModelClient([""])
    client.last_output_truncated = True
    client.last_usage = {"input_tokens": 32693, "output_tokens": 75, "total_tokens": 32768}
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._proactive("summarize the news")

    report = channel.sent[-1]
    assert "scheduled task failed" in report
    assert "cut off" in report and "32693" in report
    assert "context window" in report


async def test_proactive_auto_denies_gated_tool_on_viewed_conversation(tmp_path):
    """A firing that fell back to the viewed conversation (a channel with no conversation list)
    auto-denies a gated tool, even though streaming_conversation == _active_id would otherwise look
    foreground and wrongly prompt. Unattended turns must never prompt."""
    cfg = _config(tmp_path, confirm_tools=["update_config"])
    client = _RequestsToolOnce("update_config", {"section": "planning", "key": "plan_review", "value": "true"})
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)

    # No streaming_conversation is set by the test; _proactive sets it to _active_id (the viewed
    # conversation) itself, so only the proactive marker keeps this from prompting.
    await asyncio.wait_for(assistant._proactive("do it"), timeout=2.0)

    denied = [m for m in client.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'update_config' was not approved."


async def test_proactive_new_session_auto_denies_gated_tool(tmp_path):
    """The minted-conversation path (never the viewed one) also auto-denies."""
    cfg = _config(tmp_path, confirm_tools=["update_config"])
    client = _RequestsToolOnce("update_config", {"section": "planning", "key": "plan_review", "value": "true"})
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: client)

    await asyncio.wait_for(assistant._proactive("do it", task_name="t"), timeout=2.0)

    denied = [m for m in client.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'update_config' was not approved."


async def test_proactive_pins_agent_for_the_run(tmp_path):
    """_proactive pins its conversation's agent for the run and unpins after, mirroring _handle, so
    eviction can't drop the agent mid-run and lose the output."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient(["done"]))

    pins: list[str] = []
    unpins: list[str] = []
    orig_pin = assistant._registry.pin
    orig_unpin = assistant._registry.unpin

    def spy_pin(cid):
        pins.append(cid)
        return orig_pin(cid)

    def spy_unpin(cid):
        unpins.append(cid)
        return orig_unpin(cid)

    assistant._registry.pin = spy_pin
    assistant._registry.unpin = spy_unpin

    conversation_id = assistant._active_id
    await assistant._proactive("remind")

    assert pins == [conversation_id]
    assert unpins == [conversation_id]


async def test_assistant_persists_and_restores(tmp_path):
    cfg = _config(tmp_path)

    channel1 = FakeChannel()
    client1 = MockAsyncModelClient(["first reply"])
    assistant1 = await Assistant.create(cfg, channel1, client=client1)
    await assistant1._handle(ChannelMessage(text="remember this"), conversation_id=assistant1._active_id)
    assistant1._store.close()  # flush TinyDB

    channel2 = FakeChannel()
    client2 = MockAsyncModelClient([])  # no turn; just restore
    assistant2 = await Assistant.create(cfg, channel2, client=client2)

    restored = [m.get("content") for m in assistant2._agent.model_client.messages]
    assert "remember this" in restored
    assert "first reply" in restored


async def test_switch_away_does_not_cancel_running_turn(tmp_path):
    from kokua.core.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    conv_a = assistant._active_id

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(conv_a, TurnInfo(handle=handle, started=0.0, preview="p"))
    await assistant.new_conversation()  # switches away from A
    assert not handle.done  # A's turn keeps running
    handle.cancel()
    await asyncio.gather(handle.task, return_exceptions=True)


async def test_select_conversation_does_not_cancel_running_turn(tmp_path):
    """Mirrors the new_conversation case: select_conversation must not cancel the turn either."""
    from kokua.core.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()
    await assistant.select_conversation(conv_a)

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(conv_a, TurnInfo(handle=handle, started=0.0, preview="p"))
    await assistant.select_conversation(conv_b)  # switches away from A
    assert not handle.done
    handle.cancel()
    await asyncio.gather(handle.task, return_exceptions=True)


async def test_delete_conversation_cancels_its_own_running_turn(tmp_path):
    """delete_conversation cancels the DELETED conversation's own turn -- there is no conversation
    left for it to keep persisting to -- even if that conversation isn't the one being viewed."""
    from kokua.core.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()  # active_id now B; A is inactive
    assert assistant._active_id == conv_b

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(conv_a, TurnInfo(handle=handle, started=0.0, preview="p"))

    await assistant.delete_conversation(conv_a)
    await asyncio.sleep(0.01)
    assert handle.done  # A's own turn was cancelled
    assert assistant._active_id == conv_b  # deleting an inactive conversation leaves active untouched


async def test_background_turn_notifies_on_success_when_switched_away(tmp_path):
    """Reconciled notification key (item 1): fires exactly when the turn's conversation was muted,
    i.e. `conversation_id != self._active_id` at completion -- the same notion WebChannel's own
    muting uses once its active_conversation_id is kept in sync (see the sync test above)."""

    class _NotifyChannel(FakeChannel):
        async def send_notification(self, text: str) -> None:
            self.sent.append(f"[notify] {text}")

    channel = _NotifyChannel()
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient(["ok"]))
    conv_a = assistant._active_id
    await assistant.new_conversation()  # switch away from A before A's (background) turn runs
    assert assistant._active_id != conv_a

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=conv_a)

    assert any(s.startswith("[notify]") for s in channel.sent)


async def test_background_turn_error_notifies_with_reason(tmp_path):
    """A muted turn that errors must notify with the failure reason, not stay silent and not claim
    'reply ready' (its error reply went out muted, so the notification is the only signal the user
    gets that the background turn finished and how)."""

    class _NotifyChannel(FakeChannel):
        async def send_notification(self, text: str) -> None:
            self.sent.append(f"[notify] {text}")

    channel = _NotifyChannel()
    cfg = _config(tmp_path)
    assistant = await Assistant.create(
        cfg, channel, client_factory=lambda cid: MockAsyncModelClient([Exception("boom")])
    )
    conv_a = assistant._active_id
    await assistant.new_conversation()  # switch away from A before A's (background) turn runs
    assert assistant._active_id != conv_a

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=conv_a)

    notifies = [s for s in channel.sent if s.startswith("[notify]")]
    assert notifies, "a background failure should still notify the user"
    assert any("failed" in s.lower() for s in notifies)  # the notification carries the reason
    assert not any("reply ready" in s.lower() for s in notifies)  # not a misleading success notice


class _StopChannel(Channel):
    """Yields ``text``, waits until the turn is running, then yields '/stop'."""

    name = "fake"

    def __init__(self, started, text: str = "long task"):
        self._started = started
        self._text = text
        self.sent: list[str] = []

    async def receive(self):
        yield ChannelMessage(text=self._text, channel="fake")
        await self._started.wait()
        yield ChannelMessage(text="/stop", channel="fake")

    async def send(self, content, *, reply_to=None):
        if isinstance(content, str):
            self.sent.append(content)
            return
        async for _ in content:  # consume the stream; this is what /stop cancels
            pass


async def test_stop_cancels_in_flight_turn(tmp_path):
    client = _BlockingStreamClient()
    channel = _StopChannel(client.started)
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._serve_channel()  # reads "long task" (starts the turn), then "/stop" (cancels it)
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:  # let the cancelled turn finish its (stopped) + persist
        await asyncio.gather(info.handle.task, return_exceptions=True)

    assert "(stopped)" in channel.sent
    # The partial turn was captured for resume (the agent snapshots in its finally).
    assert any(m.get("content") == "long task" for m in assistant._agent.model_client.messages)


async def test_stop_with_no_active_turn_is_noop(tmp_path):
    class _OnlyStop(Channel):
        name = "fake"

        async def receive(self):
            yield ChannelMessage(text="/stop", channel="fake")

        async def send(self, content, *, reply_to=None):
            pass

    assistant = await Assistant.create(_config(tmp_path), _OnlyStop(), client=MockAsyncModelClient([]))
    await assistant._serve_channel()  # must not raise with no in-flight turn
    assert assistant._tracker.get(assistant._active_id) is None


async def test_two_conversations_turns_run_concurrently(tmp_path):
    """A direct TurnGate exercise: two different conversations' turns overlap, proving the gate no
    longer serializes across conversations the way the old global lock did."""
    cfg = _config(tmp_path)
    gate_events: list[str] = []

    def factory(cid):
        return MockAsyncModelClient(["done"])

    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=factory)
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()

    async def run_turn(cid):
        async with assistant._gate.turn(cid):
            gate_events.append(f"in-{cid}")
            await asyncio.sleep(0.02)
            gate_events.append(f"out-{cid}")

    await asyncio.gather(run_turn(conv_a), run_turn(conv_b))
    # Both entered before either exited -> concurrent.
    assert gate_events.index(f"in-{conv_b}") < gate_events.index(f"out-{conv_a}")


async def test_stop_cancels_active_conversation_turn(tmp_path):
    """/stop's helper cancels the tracked turn for the viewed conversation directly (no serve loop
    needed to exercise it)."""
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    from kokua.core.turn_registry import TurnInfo
    from aimu.aio import RunHandle

    async def forever():
        await asyncio.Event().wait()

    handle = RunHandle.start(forever())
    assistant._tracker.add(assistant._active_id, TurnInfo(handle=handle, started=0.0, preview="p"))
    assistant._stop_active_turn()  # helper the /stop branch calls
    await asyncio.sleep(0.01)
    assert handle.done


async def test_proactive_new_session_runs_in_fresh_conversation(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["task output"])
    )
    # Establish an active conversation with one real turn.
    await assistant._handle(ChannelMessage(text="hello there", channel="fake"), conversation_id=assistant._active_id)
    active_key = assistant._session.key
    active_len = len(assistant._session.messages)

    await assistant._proactive("run the report", task_name="report")

    # Active conversation is restored and untouched.
    assert assistant._session.key == active_key
    assert len(assistant._session.messages) == active_len
    # A new conversation exists, titled from the task, holding the task's turn.
    keys = assistant._store.list_keys()
    assert len(keys) == 2
    new_key = next(k for k in keys if k != active_key)
    new_session = assistant._store.get(new_key)
    assert new_session.metadata["title"] == "report"
    assert any(m.get("content") == "task output" for m in new_session.messages)
    # Sidebar refreshed and a notice was sent.
    assert channel.conversation_pushes
    assert any("report" in s for s in channel.sent)


async def test_proactive_stamps_minted_conversation_with_its_task_id(tmp_path):
    """Every conversation a task mints records which task minted it. Without the stamp a task's
    firings would be indistinguishable from conversations the user started, and retention would have
    nothing to prune by."""
    assistant = await Assistant.create(
        _config(tmp_path), _ConvCapturingChannel(), client_factory=lambda cid: MockAsyncModelClient(["out"])
    )

    await assistant._proactive("run it", task_name="report", task_id="task-1")
    await assistant._proactive("run it", task_name="digest", task_id="task-2")

    assert [s.metadata["title"] for s in assistant._book.sessions_for_task("task-1")] == ["report"]
    assert [s.metadata["title"] for s in assistant._book.sessions_for_task("task-2")] == ["digest"]


async def test_proactive_stamps_nothing_when_it_falls_back_to_the_viewed_conversation(tmp_path):
    """A firing with nowhere of its own to run uses the conversation the user is already viewing, so
    it must not claim that conversation for the task."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["out"]))

    await assistant._proactive("run it", task_name="report", task_id="task-1")

    assert "task_id" not in assistant._store.get(assistant._active_id).metadata


async def test_proactive_degrades_on_single_conversation_channel(tmp_path):
    channel = FakeChannel()  # no send_conversations
    client = MockAsyncModelClient(["task output"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    active_key = assistant._session.key

    await assistant._proactive("run the report", task_name="report")

    # No extra conversation; ran in place and pushed the reply.
    assert assistant._store.list_keys() == [active_key]
    assert channel.sent == ["task output"]


async def test_proactive_new_session_holds_at_most_one_gate_turn(tmp_path):
    """Regression for a nested-gate-hold deadlock: _proactive's new-session branch must not keep an
    outer gate.turn(self._active_id) held while _run_in_new_session takes its own inner
    gate.turn(new_id) -- two simultaneous reader holds from the same task can wedge against a
    concurrent gate.exclusive() (writer-preferring: the writer waits for readers to drain, but one of
    this task's own readers is stuck waiting on the other's lock, which never yields to the writer).
    Asserts a single hold is ever observed, and that none is left over afterward.
    """
    channel = _ConvCapturingChannel()
    observed: list[int] = []

    class _RecordingClient(MockAsyncModelClient):
        async def _chat(self, *args, **kwargs):
            observed.append(assistant._gate.active_turns())
            return await super()._chat(*args, **kwargs)

    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: _RecordingClient(["task output"])
    )

    await assistant._proactive("run the report", task_name="report")

    assert observed == [1]  # exactly one gate hold was active while the new session's turn ran
    assert assistant._gate.active_turns() == 0  # and none left over afterward

    # No wedged reader left behind: a concurrent exclusive() hold must complete promptly.
    async with asyncio.timeout(1.0):
        async with assistant._gate.exclusive():
            pass


async def test_proactive_new_session_auto_denies_gated_tool_and_never_hijacks_active_id(tmp_path):
    """Critical regression: _run_in_new_session must never touch self._active_id.

    Before the fix it swapped self._active_id to the new session's id for the run's duration (restored
    in a finally). With concurrent per-conversation turns that made _approve see streaming_conversation ==
    self._active_id (both the new session) -- i.e. foreground -- so a gated tool call inside a
    scheduled new-session run would PROMPT instead of auto-denying, and a concurrent user switch
    during the run would be silently clobbered back by the finally. Asserts both the auto-deny and
    that self._active_id (the viewed conversation) is untouched DURING the run, not just after.
    """
    from kokua.channels.web import streaming_conversation

    observed: dict[str, object] = {}

    class _RecordingRequestsToolOnce(_RequestsToolOnce):
        async def _chat(self, *args, **kwargs):
            # Captured mid-run (inside the agent's tool-dispatch call), not after -- the bug this
            # guards against only manifests while the scheduled turn is actually in flight.
            observed["active_id_during_run"] = assistant._active_id
            observed["streaming_conversation_during_run"] = streaming_conversation.get()
            return await super()._chat(*args, **kwargs)

    channel = _ConvCapturingChannel()
    cfg = _config(tmp_path, confirm_tools=["update_config"])
    assistant = await Assistant.create(
        cfg,
        channel,
        client_factory=lambda cid: _RecordingRequestsToolOnce(
            "update_config", {"section": "planning", "key": "plan_review", "value": "true"}
        ),
    )
    viewed = assistant._active_id

    await assistant._proactive("run the report", task_name="report")

    # Mid-run, the viewed conversation was never hijacked, and the run's own streaming context is a
    # *different* conversation than the viewed one -- the precondition for _approve to auto-deny.
    assert observed["active_id_during_run"] == viewed
    assert observed["streaming_conversation_during_run"] != viewed

    # The gated tool call the scheduled run made was denied, not prompted.
    new_id = next(k for k in assistant._store.list_keys() if k != viewed)
    new_session = assistant._store.get(new_id)
    denied = [m for m in new_session.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'update_config' was not approved."

    # And self._active_id is still the viewed conversation after the run completes.
    assert assistant._active_id == viewed


async def test_proactive_prunes_the_tasks_conversations_past_its_cap_oldest_first(tmp_path):
    """The point of a cap: a task that fires often keeps its most recent runs and nothing older,
    without the user deleting the pile by hand."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out"])
    )
    active_key = assistant._active_id

    kept: list[str] = []
    for _ in range(3):
        await assistant._proactive("run", task_name="digest", task_id="t1", max_conversations=2)
        kept = [session.key for session in assistant._book.sessions_for_task("t1")]

    assert len(kept) == 2  # the third firing dropped the first run
    assert all(key in assistant._store.list_keys() for key in kept)
    assert active_key in assistant._store.list_keys()  # the user's own conversation is untouched


async def test_proactive_at_a_cap_of_one_replaces_the_previous_run(tmp_path):
    """A cap of one is the old "latest" behavior: each firing mints a conversation and the one before
    it goes, never the one this firing just used."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out"])
    )

    await assistant._proactive("first run", task_name="digest", task_id="t1", max_conversations=1)
    first_key = assistant._book.sessions_for_task("t1")[0].key
    await assistant._proactive("second run", task_name="digest", task_id="t1", max_conversations=1)

    kept = [session.key for session in assistant._book.sessions_for_task("t1")]
    assert len(kept) == 1 and kept[0] != first_key
    assert first_key not in assistant._store.list_keys()
    assert kept[0] in assistant._store.list_keys()


async def test_proactive_with_an_unlimited_cap_keeps_every_run(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out"])
    )

    for _ in range(3):
        await assistant._proactive("run", task_name="digest", task_id="t1", max_conversations=0)

    assert len(assistant._book.sessions_for_task("t1")) == 3


async def test_proactive_keeps_the_previous_run_when_the_new_one_fails(tmp_path):
    """The last good run has to outlive a bad one, at the tightest cap there is.

    A cap of 1 is the case that pins the eviction order: both runs cannot survive it, and evicting
    strictly oldest-first would keep the failure and drop the report the user actually wants. The first
    client goes to the conversation ``create`` opens, so the two firings take the second and third.
    """
    channel = _ConvCapturingChannel()
    clients = iter([MockAsyncModelClient([]), MockAsyncModelClient(["out1"]), _FailingClient([])])
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda cid: next(clients))

    await assistant._proactive("first run", task_name="digest", task_id="t1", max_conversations=1)
    first_key = assistant._book.sessions_for_task("t1")[0].key
    await assistant._proactive("second run", task_name="digest", task_id="t1", max_conversations=1)

    assert first_key in assistant._store.list_keys()  # the last good run survived the failure
    assert len(assistant._book.sessions_for_task("t1")) == 1  # and the failure is what the cap evicted


async def test_proactive_tolerates_a_run_the_user_already_deleted(tmp_path):
    """The user can delete a run themselves between firings, so pruning must not be the thing that
    takes down the scheduler job (invariant 6)."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out"])
    )
    await assistant._proactive("first", task_name="digest", task_id="t1", max_conversations=1)
    await assistant.delete_conversation(assistant._book.sessions_for_task("t1")[0].key)

    await assistant._proactive("second", task_name="digest", task_id="t1", max_conversations=1)

    kept = assistant._book.sessions_for_task("t1")
    assert len(kept) == 1 and kept[0].key in assistant._store.list_keys()


async def test_proactive_prunes_only_the_conversations_of_the_task_that_fired(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out"])
    )
    await assistant._proactive("other task", task_name="other", task_id="t2", max_conversations=1)
    other_key = assistant._book.sessions_for_task("t2")[0].key

    for _ in range(2):
        await assistant._proactive("mine", task_name="digest", task_id="t1", max_conversations=1)

    assert other_key in assistant._store.list_keys()


async def test_proactive_prunes_nothing_when_it_falls_back_to_the_viewed_conversation(tmp_path):
    """A channel with no conversation list mints nothing, so the task owns nothing to prune -- and the
    conversation it ran in belongs to the user, who must not lose it to a cap."""
    channel = FakeChannel()  # no send_conversations
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["out1", "out2"]))
    active_key = assistant._active_id

    for _ in range(2):
        await assistant._proactive("run", task_name="digest", task_id="t1", max_conversations=1)

    assert assistant._store.list_keys() == [active_key]


class _FailingClient(MockAsyncModelClient):
    """Raises on run, standing in for an unreachable or misconfigured model server."""

    async def chat(self, *args, **kwargs):
        raise RuntimeError("model exploded")

    async def chat_streamed(self, *args, **kwargs):
        raise RuntimeError("model exploded")


async def test_proactive_surfaces_errors_and_keeps_the_conversation_it_minted(tmp_path):
    """A failing scheduled firing must not escape into the scheduler job, which has no handler.

    Before the proactive paths were unified, only the run-in-place path had error handling: the
    minting branch returned early, past _proactive's handlers, so a model error propagated into
    scheduling._fire_job and would take the scheduler down. The conversation the failed firing minted
    stays, so there is something to read that says what went wrong.
    """
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda cid: _FailingClient([]))
    active_key = assistant._active_id

    await assistant._proactive("do it", task_name="digest", task_id="t1")

    kept = assistant._book.sessions_for_task("t1")
    assert len(kept) == 1 and kept[0].key != active_key
    assert any("failed" in str(text) for text in channel.sent)  # the user is told
    assert assistant._active_id == active_key  # the viewed conversation is untouched


async def test_proactive_surfaces_errors_on_the_fallback_path(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda cid: _FailingClient([]))
    assert await assistant._proactive("do it") is None  # swallowed, not raised
    assert any("failed" in str(text) for text in channel.sent)


class _SpawningClient(MockAsyncModelClient):
    """A mock client that reports a sub-agent spawn mid-turn, standing in for AIMU's dispatch.

    ``chat(stream=True)`` calls the base client's ``_chat`` twice: once with ``stream=True`` to get
    the streaming wrapper, then again with ``stream=False`` from inside that wrapper to do the real
    work (the base mock's own message-appending is likewise skipped on the first call). Reporting
    only on the ``stream=False`` call keeps this fake to one spawn per turn regardless of whether the
    turn streams, matching a real spawn (tied to one tool dispatch, not to how many times the model
    client's transport method is entered).
    """

    def __init__(self, reply: str = "delegated."):
        super().__init__([reply])
        self.reporter = None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if self.reporter is not None and not stream:
            await self.reporter.spawned("r-1", "researcher", "find X")
            await self.reporter.finished("r-1", "the answer", None)
        return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)


class _InterleavingSpawningClient(MockAsyncModelClient):
    """Spawns a sub-agent under a request-specific id (looked up by the turn's own message text), and
    yields control between the spawn and its finish. Two turns sharing this one client -- as the mock
    `client_factory` does below, one client standing in for every conversation's -- genuinely
    interleave their reporter calls this way, rather than one completing before the other starts."""

    def __init__(self, spawn_ids: dict[str, str], reply: str = "delegated."):
        super().__init__([reply, reply])
        self.reporter = None
        self._spawn_ids = spawn_ids

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        spawn_id = self._spawn_ids.get(user_message)
        if self.reporter is not None and spawn_id is not None and not stream:
            await self.reporter.spawned(spawn_id, "researcher", "find X")
            await asyncio.sleep(0)  # yield, so the other conversation's turn interleaves right here
            await self.reporter.finished(spawn_id, "the answer", None)
        return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)


class _SpawnsThenHangsClient(MockAsyncModelClient):
    """Reports a spawn as running, then hangs until the turn task is cancelled."""

    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()
        self.reporter = None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        self.messages.append({"role": "user", "content": user_message})
        if self.reporter is not None:
            await self.reporter.spawned("r-1", "researcher", "find X")
        self.started.set()
        await asyncio.Event().wait()  # hang until cancelled


async def test_turn_records_its_subagent_events_under_its_user_index(tmp_path):
    """The events key to the turn's user message; that index is what places the cards on reload."""
    client = _SpawningClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._handle(ChannelMessage(text="delegate this", channel="fake"), conversation_id=assistant._active_id)

    metadata = assistant._store.get(assistant._active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1", "r-1"]
    assert metadata["subagent"]["0"][0]["task"] == "find X"


async def test_a_spawn_card_records_the_workers_own_model(tmp_path):
    """The card is what the stored JSON replays, and a worker need not run on the model that answered
    the turn: this is the only record of which model produced its part."""
    agents = example_agents()
    agents["researcher"].model = "ollama:qwen3:32b"
    client = _SpawningClient()
    config = _config(tmp_path, model="ollama:qwen3:8b", agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._handle(ChannelMessage(text="delegate this", channel="fake"), conversation_id=assistant._active_id)

    card = assistant._store.get(assistant._active_id).metadata["subagent"]["0"][0]
    assert card["model"] == "ollama:qwen3:32b"


async def test_turn_records_the_model_that_answered_it_under_its_user_index(tmp_path):
    """The stored JSON has to say which model produced an answer, and a conversation outlives the
    config: [assistant].model can be edited between two turns of the same conversation."""
    assistant = await Assistant.create(
        _config(tmp_path, model="ollama:qwen3:8b"), FakeChannel(), client=MockAsyncModelClient(["hi"])
    )

    await assistant._handle(ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id)

    assert assistant._store.get(assistant._active_id).metadata["model"]["0"] == "ollama:qwen3:8b"


async def test_a_turn_records_the_entry_agents_own_model_over_the_default(tmp_path):
    agents = example_agents()
    agents["assistant"].model = "ollama:qwen3:32b"
    config = _config(tmp_path, model="ollama:qwen3:8b", agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["hi"]))

    await assistant._handle(ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id)

    assert assistant._store.get(assistant._active_id).metadata["model"]["0"] == "ollama:qwen3:32b"


async def test_a_turn_on_a_conversation_not_being_viewed_still_records(tmp_path):
    """Switching away mutes the frames but must not lose the trace: the record is what the user
    comes back to."""
    client = _SpawningClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=lambda cid: client)
    client.reporter = assistant._subagent_reporter
    background = assistant._book.new_session()

    await assistant._handle(ChannelMessage(text="delegate this", channel="fake"), conversation_id=background.key)

    assert background.key != assistant._active_id
    assert "subagent" in assistant._store.get(background.key).metadata


async def test_two_conversations_concurrent_spawns_stay_isolated(tmp_path):
    """Cross-conversation isolation is the premise the whole feature rests on: one reporter and one
    `subagent_events` ContextVar mechanism serve every conversation's turns, and those turns run
    concurrently by default. The sequential-await test above can't catch a leak between conversations;
    this drives both turns as real overlapping tasks (the same class of bug fixed in
    SubagentReporter._record, which used to let a reporter-level slot bleed across turns)."""
    client = _InterleavingSpawningClient({"delegate a": "r-a", "delegate b": "r-b"})
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=lambda cid: client)
    client.reporter = assistant._subagent_reporter
    conv_a = assistant._active_id
    conv_b = assistant._book.new_session().key

    await asyncio.gather(
        assistant._handle(ChannelMessage(text="delegate a", channel="fake"), conversation_id=conv_a),
        assistant._handle(ChannelMessage(text="delegate b", channel="fake"), conversation_id=conv_b),
    )

    meta_a = assistant._store.get(conv_a).metadata
    meta_b = assistant._store.get(conv_b).metadata
    assert [event["id"] for event in meta_a["subagent"]["0"]] == ["r-a", "r-a"]
    assert [event["id"] for event in meta_b["subagent"]["0"]] == ["r-b", "r-b"]


async def test_a_stopped_turn_records_the_events_it_produced(tmp_path):
    """`/stop` mid-spawn: the cancelled turn persists the card it had opened."""
    client = _SpawnsThenHangsClient()
    channel = _StopChannel(client.started)
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._serve_channel()  # reads "long task" (starts the turn), then "/stop" (cancels it)
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:  # let the cancelled turn finish its (stopped) + persist
        await asyncio.gather(info.handle.task, return_exceptions=True)

    metadata = assistant._store.get(assistant._active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1"]


class _CancellingRichRunner:
    """Publishes an index and a sub-agent event, then is cancelled -- standing in for `/stop` arriving
    mid-turn without needing a real cancellation, since raising `CancelledError` from `run_turn` is
    indistinguishable to `reactive`'s `except asyncio.CancelledError` from one delivered by the task."""

    def __init__(self, ctx, reporter):
        self.ctx = ctx
        self.reporter = reporter

    async def run_turn(self):
        self.ctx.publish_user_index(0)
        await self.reporter.spawned("r-1", "researcher", "find X")
        raise asyncio.CancelledError()


async def test_a_cancelled_rich_workflow_still_records_its_published_events(tmp_path):
    """The `finally` around a rich workflow's `run_turn` exists so a workflow that raises still
    anchors its sub-agent cards at the index it published, rather than at -1 (which would no-op the
    recording, per `record_subagent_events`). This covers invariant 5 for the workflow branch's shape
    itself, independent of any one workflow; `test_a_stopped_planned_turn_records_the_events_it_produced`
    covers the same invariant for planning specifically."""
    channel = FakeChannel()
    client = MockAsyncModelClient(["unused"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    workflow = Workflow(
        name="cancels",
        description="C.",
        command="cancels",
        usage="/cancels <x>",
        build=lambda ctx: _CancellingRichRunner(ctx, assistant._subagent_reporter),
    )

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id, workflow=workflow
    )

    metadata = assistant._store.get(assistant._active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1"]
    assert channel.sent == ["(stopped)"]


class _FailingRichRunner:
    """Publishes an index and a sub-agent event, then fails outright (not cancelled) -- the other
    branch that reaches the same `finally`."""

    def __init__(self, ctx, reporter):
        self.ctx = ctx
        self.reporter = reporter

    async def run_turn(self):
        self.ctx.publish_user_index(0)
        await self.reporter.spawned("r-1", "researcher", "find X")
        raise RuntimeError("boom")


async def test_a_failed_rich_workflow_still_records_its_published_events(tmp_path):
    """Same shape as the cancelled case, through the generic-error branch: the events a workflow
    published before raising must still land, and the user still gets a failure message."""
    channel = FakeChannel()
    client = MockAsyncModelClient(["unused"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    workflow = Workflow(
        name="fails",
        description="F.",
        command="fails",
        usage="/fails <x>",
        build=lambda ctx: _FailingRichRunner(ctx, assistant._subagent_reporter),
    )

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id, workflow=workflow
    )

    metadata = assistant._store.get(assistant._active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1"]
    assert channel.sent == ["Sorry, the request failed: RuntimeError: boom"]


class _PlansThenSpawnsAndHangsClient(MockAsyncModelClient):
    """Drafts a plan, then reports a spawn from the executor and hangs until the turn is cancelled.

    The planner call goes through the base mock (a planned turn's first run is non-streaming on a
    channel without live activity), so only the executor stands in for a delegation.
    """

    def __init__(self):
        super().__init__(["THE PLAN"])
        self.started = asyncio.Event()
        self.reporter = None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if self._call_count == 0:
            return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)
        self.messages.append({"role": "user", "content": user_message})
        if self.reporter is not None:
            await self.reporter.spawned("r-1", "researcher", "find X")
        self.started.set()
        await asyncio.Event().wait()  # hang until cancelled


async def test_a_stopped_planned_turn_records_the_events_it_produced(tmp_path):
    """`/stop` mid-spawn on a `/plan` turn: the card is anchored where a completed planned turn's
    would be. The planning branch used to take its index from the result the run returns, which a
    cancelled run never produces, so the index stayed -1 and the recording silently no-opped."""
    client = _PlansThenSpawnsAndHangsClient()
    channel = _StopChannel(client.started, text="/plan long task")
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._serve_channel()  # reads "/plan long task", then "/stop" (cancels the turn)
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:  # let the cancelled turn finish its (stopped) + persist
        await asyncio.gather(info.handle.task, return_exceptions=True)

    metadata = assistant._store.get(assistant._active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1"]
    messages = assistant._agent.model_client.messages
    assert messages[0]["content"] == "long task"  # the card's anchor is the user's own words


class _SpawnsWhilePlanningThenHangsClient(MockAsyncModelClient):
    """Reports a spawn from the planner, then hangs in the executor before it appends anything.

    The planner's messages are rolled back (planning is scratch work), so at cancellation there is no
    user message anywhere: the events are real but have nothing to anchor to.
    """

    def __init__(self):
        super().__init__(["THE PLAN"])
        self.started = asyncio.Event()
        self.reporter = None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if self._call_count == 0:
            if self.reporter is not None:
                await self.reporter.spawned("r-1", "researcher", "find X")
            return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)
        self.started.set()
        await asyncio.Event().wait()  # hang until cancelled


async def test_a_planned_turn_cancelled_before_the_executor_commits_records_nothing(tmp_path):
    """The streaming path publishes its index only once the executor's user message is really there.
    Publishing it up front would file these events at an index the next turn's user message takes."""
    client = _SpawnsWhilePlanningThenHangsClient()
    channel = _StopChannel(client.started, text="/plan long task")
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._serve_channel()
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:
        await asyncio.gather(info.handle.task, return_exceptions=True)

    assert assistant._agent.model_client.messages == []
    assert "subagent" not in assistant._store.get(assistant._active_id).metadata


class _PhaseStopChannel(_StopChannel):
    """A phase-capable ``_StopChannel``, so a planned turn takes the reviewed (trace) path."""

    async def send_phase(self, label, detail="") -> None:
        pass


async def test_a_stopped_reviewed_planned_turn_with_no_answer_records_nothing(tmp_path):
    """The reviewed path rolls back to the pre-execution messages when it has no answer to commit, so
    a run cancelled that early has no anchor: the index it would publish is the one the *next* turn's
    user message will occupy, which would show this turn's cards on that one."""
    client = _PlansThenSpawnsAndHangsClient()
    channel = _PhaseStopChannel(client.started, text="/plan long task")
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(show_reasoning=True)), channel, client=client
    )
    client.reporter = assistant._subagent_reporter

    await assistant._serve_channel()
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:
        await asyncio.gather(info.handle.task, return_exceptions=True)

    assert assistant._agent.model_client.messages == []  # nothing committed...
    assert "subagent" not in assistant._store.get(assistant._active_id).metadata  # ...so nothing filed


async def test_a_rejected_plan_records_nothing(tmp_path):
    """A rejected plan commits no user message, so a spawn the planner made has nothing to anchor to.
    Recording it at the pre-execution length would file it under whatever the next turn commits there."""
    client = _SpawningClient("THE PLAN")
    assistant = await Assistant.create(
        _config(tmp_path, toolset_settings=planning_settings(plan_review=True)), FakeChannel(), client=client
    )
    client.reporter = assistant._subagent_reporter
    active_id = assistant._active_id

    turn = asyncio.create_task(
        assistant._handle(
            ChannelMessage(text="do X", channel="fake"), conversation_id=active_id, workflow=PLANNING_WORKFLOW
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

    assert "subagent" not in assistant._store.get(active_id).metadata


class _CancelsOnStoppedSendChannel(Channel):
    """Raises ``CancelledError`` from the "(stopped)" notice, standing in for a second cancellation
    delivered while that send is in flight -- exactly the race invariant 5 is about."""

    name = "fake"

    def __init__(self):
        self.sent: list[str] = []

    async def receive(self):
        return
        yield  # pragma: no cover - never reached; this channel is driven directly, not via serve

    async def send(self, content, *, reply_to=None):
        if content == "(stopped)":
            raise asyncio.CancelledError()
        if isinstance(content, str):
            self.sent.append(content)
            return
        async for _ in content:  # pragma: no cover - not exercised by this test
            pass


async def test_a_second_cancellation_during_the_stopped_send_still_records(tmp_path):
    """Regression for invariant 5: a cancellation racing the '(stopped)' notice must not skip the
    record call. Before the fix, `_record_provenance` ran after that send, so this second
    CancelledError (an ordinary BaseException, uncaught by the send's `except Exception`) propagated
    straight past it and the spawn's card was lost."""
    client = _SpawnsThenHangsClient()
    channel = _CancelsOnStoppedSendChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    client.reporter = assistant._subagent_reporter
    active_id = assistant._active_id

    task = asyncio.create_task(
        assistant._handle(ChannelMessage(text="long task", channel="fake"), conversation_id=active_id)
    )
    await client.started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)  # the second CancelledError propagates out

    metadata = assistant._store.get(active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1"]


async def test_a_proactive_turn_records_its_subagent_events(tmp_path):
    """A scheduled task's delegation is recorded against the message index the run started at."""
    client = _SpawningClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._proactive("delegate this")

    metadata = assistant._store.get(assistant._active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1", "r-1"]


# --- the first turn of a conversation, where a real client seeds the system message ----------------
#
# The clients below mix in _SeedsSystemMessage, so their transcripts look like a real run's: the first
# turn is [system, user, assistant, ...] and its user message is at 1, not at the pre-run length of 0.
# That is the position replay_items looks a turn's cards up by, so anything else drops them.


def _user_positions(messages: list[dict]) -> list[int]:
    """The real positions of the user messages, as ``replay_items`` enumerates them."""
    return [index for index, message in enumerate(messages) if message.get("role") == "user"]


class _SeedingSpawningClient(_SeedsSystemMessage, MockAsyncModelClient):
    """``_SpawningClient`` that also seeds the system message, with one spawn per turn under a
    per-turn id so a recorded key can be traced back to the turn that produced it."""

    def __init__(self, turns: int = 2):
        super().__init__(["delegated."] * turns)
        self.reporter = None
        self.spawns = 0

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if self.reporter is not None and not stream:  # see _SpawningClient on the two _chat entries
            self.spawns += 1
            await self.reporter.spawned(f"r-{self.spawns}", "researcher", "find X")
            await self.reporter.finished(f"r-{self.spawns}", "the answer", None)
        return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)


class _SeedingPlansThenSpawnsClient(_SeedsSystemMessage, MockAsyncModelClient):
    """Drafts a plan, then reports a spawn from the executor, seeding the system message as a real
    client does. Only the executor delegates: the planning exchange is rolled back."""

    def __init__(self):
        super().__init__(["THE PLAN", "delegated."])
        self.reporter = None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if self.reporter is not None and self._call_count > 0 and not stream:
            await self.reporter.spawned("r-1", "researcher", "find X")
            await self.reporter.finished("r-1", "the answer", None)
        return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)


class _SeedingSpawnsThenHangsClient(_SeedsSystemMessage, _SpawnsThenHangsClient):
    """``_SpawnsThenHangsClient`` on a first turn's transcript."""


async def test_a_first_turns_cards_anchor_to_its_user_message_past_the_system_message(tmp_path):
    """The index came from the transcript length captured before the run, but the first turn's own run
    appends the system message ahead of its user message: the cards were filed under "0", which is the
    system message, and the reload replay (which only consults user positions) never saw them."""
    client = _SeedingSpawningClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter
    active_id = assistant._active_id

    await assistant._handle(ChannelMessage(text="delegate this", channel="fake"), conversation_id=active_id)

    session = assistant._store.get(active_id)
    assert session.messages[0]["role"] == "system"  # the seeding the mock used to skip
    assert _user_positions(session.messages) == [1]
    assert [event["id"] for event in session.metadata["subagent"]["1"]] == ["r-1", "r-1"]

    # The second turn, on the same conversation: no seeding this time, so the index is unchanged from
    # what the pre-run length would have given, and it must still land on the new user message.
    await assistant._handle(ChannelMessage(text="delegate again", channel="fake"), conversation_id=active_id)

    session = assistant._store.get(active_id)
    assert _user_positions(session.messages) == [1, 3]
    assert sorted(session.metadata["subagent"]) == ["1", "3"]
    assert [event["id"] for event in session.metadata["subagent"]["3"]] == ["r-2", "r-2"]


async def test_a_first_planned_turns_cards_anchor_to_its_user_message(tmp_path):
    """The streaming plan path guarded on a user message sitting exactly at the pre-run length, so on
    a first turn the guard failed outright: no index (the cards were dropped) and no rewrite (the
    transcript kept the executor's scaffolding prompt in place of the request)."""
    client = _SeedingPlansThenSpawnsClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter
    active_id = assistant._active_id

    await assistant._handle(
        ChannelMessage(text="do X", channel="fake"), conversation_id=active_id, workflow=PLANNING_WORKFLOW
    )

    session = assistant._store.get(active_id)
    assert _user_positions(session.messages) == [1]
    assert session.messages[1]["content"] == "do X"  # the user's own words, not EXECUTE_PROMPT
    assert [event["id"] for event in session.metadata["subagent"]["1"]] == ["r-1", "r-1"]


async def test_a_proactive_first_turns_cards_anchor_to_its_user_message(tmp_path):
    """A scheduled task firing into a conversation that has never run a turn seeds the system message
    too, so the unattended path needs the same resolution the reactive one does."""
    client = _SeedingSpawningClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._proactive("delegate this")

    session = assistant._store.get(assistant._active_id)
    assert _user_positions(session.messages) == [1]
    assert [event["id"] for event in session.metadata["subagent"]["1"]] == ["r-1", "r-1"]


async def test_a_stopped_first_turns_cards_anchor_to_its_user_message(tmp_path):
    """`/stop` on a first turn: the agent's snapshot holds [system, user], so the partial turn's card
    anchors at 1 just as a completed one would."""
    client = _SeedingSpawnsThenHangsClient()
    channel = _StopChannel(client.started)
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._serve_channel()  # reads "long task" (starts the turn), then "/stop" (cancels it)
    info = assistant._tracker.get(assistant._active_id)
    if info is not None:  # let the cancelled turn finish its (stopped) + persist
        await asyncio.gather(info.handle.task, return_exceptions=True)

    session = assistant._store.get(assistant._active_id)
    assert _user_positions(session.messages) == [1]
    assert [event["id"] for event in session.metadata["subagent"]["1"]] == ["r-1"]


class _CatchUpChannel(FakeChannel):
    """Logs the catch-up bookkeeping calls and the sidebar push, in arrival order."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def begin_catch_up(self, conversation_id, text, image_paths=None):
        self.calls.append(("begin", conversation_id, text))

    def end_catch_up(self, conversation_id):
        self.calls.append(("end", conversation_id))

    async def send_conversations(self, items):
        self.calls.append(("conversations",))


async def test_a_turn_opens_a_catch_up_record_and_ends_it_at_the_persist(tmp_path):
    """The record stands in for the turn's output until the store holds it, so it must end next to that
    write -- before the sidebar push that follows it -- or a switch-in landing in between replays the
    turn twice, once from history and once from the record."""
    channel = _CatchUpChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["ok"]))
    active_id = assistant._active_id

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=active_id)

    assert channel.calls[0] == ("begin", active_id, "hi")
    assert channel.calls[1] == ("end", active_id)  # at the persist, ahead of the first-turn title push
    assert ("conversations",) in channel.calls[2:]


async def test_a_turn_that_fails_before_persisting_still_ends_its_catch_up_record(tmp_path):
    """Otherwise the record lingers and the next switch-in replays a phantom user bubble for a turn
    that produced nothing."""
    channel = _CatchUpChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    active_id = assistant._active_id
    assistant._book.persist = lambda conversation_id: (_ for _ in ()).throw(RuntimeError("store is gone"))

    with pytest.raises(RuntimeError):  # proves the persist never reached its own end_catch_up call
        await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=active_id)

    assert ("end", active_id) in channel.calls


async def test_an_unattended_turn_opens_a_catch_up_record_for_the_conversation_it_runs_in(tmp_path):
    """A scheduled task running in its own conversation is by definition not the conversation being
    viewed, so every frame it produces is muted -- and a sub-agent card is the only frame such a turn
    produces at all. Without a record, switching in mid-task shows none of the work, and the spawn's
    later `append` frames then reach a page holding no card to update."""
    channel = _CatchUpChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["task output"])
    )
    active_id = assistant._active_id

    await assistant._proactive("run the report", task_name="report")

    task_id = next(key for key in assistant._store.list_keys() if key != active_id)
    assert ("begin", task_id, "run the report") in channel.calls
    # Ended at the persist, so a switch-in landing after it replays the store alone, not both.
    assert channel.calls.index(("end", task_id)) > channel.calls.index(("begin", task_id, "run the report"))


async def test_a_turn_records_what_it_cost(tmp_path):
    """The sink is attached for the turn, so its model calls land in the conversation's metadata."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["hello"]))

    await assistant._handle(ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id)

    usage = assistant._store.get(assistant._active_id).metadata["usage"]
    record = next(iter(usage.values()))
    assert record["calls"] >= 1
    assert record["wall_seconds"] >= 0
    # The mock reports no usage, which is the case a local server also presents.
    assert "input_tokens" not in record


async def test_the_turns_accumulator_is_cleared_afterwards(tmp_path):
    """Left set, a later turn on another conversation would accumulate into a finished turn's
    record. The ContextVar reset in the `finally` is what prevents it."""
    from kokua.core.metrics import current_metrics

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["hello"]))

    await assistant._handle(ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id)

    assert current_metrics.get() is None


async def test_two_conversations_turns_are_recorded_separately(tmp_path):
    """The isolation that matters in practice: a backgrounded turn on one conversation must not
    have its cost folded into the turn the user is watching on another."""
    client = _InterleavingSpawningClient({"delegate a": "r-a", "delegate b": "r-b"})
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=lambda cid: client)
    client.reporter = assistant._subagent_reporter
    conv_a = assistant._active_id
    conv_b = assistant._book.new_session().key

    await asyncio.gather(
        assistant._handle(ChannelMessage(text="delegate a", channel="fake"), conversation_id=conv_a),
        assistant._handle(ChannelMessage(text="delegate b", channel="fake"), conversation_id=conv_b),
    )

    usage_a = assistant._store.get(conv_a).metadata["usage"]
    usage_b = assistant._store.get(conv_b).metadata["usage"]
    record_a = next(iter(usage_a.values()))
    record_b = next(iter(usage_b.values()))
    # Exact counts, not >=1: a leak that folds one conversation's call into the other's record
    # still satisfies >=1 on both sides, which is why that weaker assertion cannot catch it.
    assert record_a["calls"] == 1
    assert record_b["calls"] == 1


async def test_a_turn_records_the_thinking_it_ran_at(tmp_path):
    agents = example_agents()
    agents["assistant"].thinking = "high"
    config = _config(tmp_path, thinking="low", agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["hi"]))

    await assistant._handle(ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id)

    assert assistant._store.get(assistant._active_id).metadata["thinking"]["0"] == "high"


async def test_a_turn_runs_at_the_effort_its_message_asked_for(tmp_path):
    """A per-turn request beats both config tiers, and the run is what has to see it: the agent's own
    `thinking` field stays at the configured effort, so only the per-run argument carries the request."""
    seen = []
    agents = example_agents()
    agents["assistant"].thinking = "low"
    config = _config(tmp_path, thinking="low", agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["hi"]))
    agent = assistant._book.agent_for(assistant._active_id)
    original_run = agent.run

    async def recording_run(task, **kwargs):
        seen.append(kwargs.get("thinking"))
        return await original_run(task, **kwargs)

    agent.run = recording_run

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake", metadata={"thinking": "high"}),
        conversation_id=assistant._active_id,
    )

    assert seen == ["high"]
    assert agent.thinking == "low", "the request is per run, not a mutation of the agent"


async def test_a_turn_records_the_effort_its_message_asked_for(tmp_path):
    config = _config(tmp_path, thinking="low")
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["hi"]))

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake", metadata={"thinking": "high"}),
        conversation_id=assistant._active_id,
    )

    assert assistant._store.get(assistant._active_id).metadata["thinking"]["0"] == "high"


async def test_a_message_can_ask_for_no_reasoning_at_all(tmp_path):
    """`off` is a request, not the absence of one, so it has to beat a configured level and be
    distinguishable in the record from a turn that asked for nothing."""
    config = _config(tmp_path, thinking="high")
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["hi"]))

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake", metadata={"thinking": "off"}),
        conversation_id=assistant._active_id,
    )

    assert assistant._store.get(assistant._active_id).metadata["thinking"]["0"] is False


async def test_an_unrecognized_effort_request_leaves_the_configured_one_in_force(tmp_path):
    config = _config(tmp_path, thinking="low")
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["hi"]))

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake", metadata={"thinking": "xhigh"}),
        conversation_id=assistant._active_id,
    )

    assert assistant._store.get(assistant._active_id).metadata["thinking"]["0"] == "low"


class _QuietRichRunner:
    """A rich workflow that commits a turn and does nothing else, for testing what the core records
    around a workflow rather than what any particular workflow does."""

    def __init__(self, ctx):
        self.ctx = ctx

    async def run_turn(self):
        self.ctx.publish_user_index(0)
        return WorkflowResult(committed=True, user_index=0)


async def test_a_workflow_turn_ignores_and_does_not_record_an_effort_request(tmp_path):
    """A workflow runs its own agents at their declared efforts, so a request that rides a workflow turn
    applies to nothing. Recording it anyway would make the transcript claim an effort the turn never
    ran at, which is the one thing this record exists to get right."""
    config = _config(tmp_path, thinking="low")
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["unused"]))
    workflow = Workflow(
        name="quiet",
        description="Q.",
        command="quiet",
        usage="/quiet <x>",
        build=lambda ctx: _QuietRichRunner(ctx),
    )

    await assistant._handle(
        ChannelMessage(text="hello", channel="fake", metadata={"thinking": "high"}),
        conversation_id=assistant._active_id,
        workflow=workflow,
    )

    assert assistant._store.get(assistant._active_id).metadata["thinking"]["0"] == "low"


async def test_a_spawn_card_records_the_workers_own_thinking(tmp_path):
    """A worker need not reason at the same effort as the agent that spawned it, so the card is the only
    record of what its part ran at."""
    agents = example_agents()
    agents["researcher"].thinking = False
    client = _SpawningClient()
    config = _config(tmp_path, thinking="high", agents=agents)
    assistant = await Assistant.create(config, FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._handle(ChannelMessage(text="delegate this", channel="fake"), conversation_id=assistant._active_id)

    card = assistant._store.get(assistant._active_id).metadata["subagent"]["0"][0]
    assert card["thinking"] is False


async def test_a_failed_firing_persists_the_partial_transcript_it_produced(tmp_path):
    """A scheduled run that fails must leave its conversation readable.

    The unattended path used to reach ``_persist`` only where the run returned normally, so a firing
    that raised left the conversation it minted exactly as minted: zero messages, ``updated_at`` still
    equal to ``created_at``, and everything the run had done up to the failure lost with the agent. The
    prompt is the floor of what has to survive -- a model client appends the user turn before it sends
    the request, so it is on the transcript whatever the request does next.
    """
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path),
        channel,
        client_factory=lambda cid: MockAsyncModelClient([RuntimeError("out of context")]),
    )

    await assistant._proactive("scan the transcripts", task_name="digest", task_id="t1")

    session = assistant._book.sessions_for_task("t1")[0]
    assert [m.get("content") for m in session.messages if m.get("role") == "user"] == ["scan the transcripts"]
    assert session.metadata["updated_at"] > session.metadata["created_at"]


async def test_a_failed_firing_records_why_it_stopped_in_its_own_conversation(tmp_path):
    """An unattended run's ``_report`` line goes to whichever conversation the user is viewing, so the
    run's own conversation is the only durable place the reason can live.

    Recorded in metadata under the turn's user-message index, the way the model and the trace are,
    rather than appended as a message: everything in ``session.messages`` is what this conversation's
    agent rebuilds from, and a synthesized assistant turn would come back to the model as its own
    prior words.
    """
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path),
        channel,
        client_factory=lambda cid: MockAsyncModelClient([RuntimeError("out of context")]),
    )

    await assistant._proactive("scan the transcripts", task_name="digest", task_id="t1")

    failure = assistant._book.sessions_for_task("t1")[0].metadata["failure"]
    (reason,) = failure.values()
    assert "out of context" in reason


async def test_a_failed_firing_tags_its_partial_messages_as_proactive(tmp_path):
    """The provenance tag is what keeps a replayed unattended turn from reading as something the user
    typed. Applied in the same place the transcript is snapshotted, so a partial turn is tagged too:
    tagging only where the run returned normally left the failed path's messages untagged the moment
    they started being persisted."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path),
        channel,
        client_factory=lambda cid: MockAsyncModelClient([RuntimeError("out of context")]),
    )

    await assistant._proactive("scan the transcripts", task_name="digest", task_id="t1")

    session = assistant._book.sessions_for_task("t1")[0]
    assert session.messages
    assert all(m.get(PROVENANCE_KEY) == PROVENANCE_PROACTIVE for m in session.messages)


async def test_a_task_that_fails_every_firing_still_honours_its_cap(tmp_path):
    """Retention used to run only where the firing succeeded, so a task failing on every firing minted
    an unbounded pile of conversations the cap was supposed to cover."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path),
        channel,
        client_factory=lambda cid: MockAsyncModelClient([RuntimeError("boom")]),
    )

    for _ in range(4):
        await assistant._proactive("run", task_name="digest", task_id="t1", max_conversations=2)

    assert len(assistant._book.sessions_for_task("t1")) == 2


async def test_retention_evicts_a_failed_run_before_a_successful_one(tmp_path):
    """What lets retention run on the failure path without costing the user their last good report.

    Ordering candidates failed-first is the whole mechanism: at a cap of 1 a strictly oldest-first
    eviction would drop the successful run in favour of the failure that followed it.
    """
    channel = _ConvCapturingChannel()
    clients = iter(
        [
            MockAsyncModelClient([RuntimeError("boom")]),
            MockAsyncModelClient(["the report"]),
            MockAsyncModelClient([RuntimeError("boom")]),
        ]
    )
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda cid: next(clients))

    await assistant._proactive("run", task_name="digest", task_id="t1", max_conversations=2)
    await assistant._proactive("run", task_name="digest", task_id="t1", max_conversations=2)
    good_key = next(s.key for s in assistant._book.sessions_for_task("t1") if not s.metadata.get("failure"))
    await assistant._proactive("run", task_name="digest", task_id="t1", max_conversations=2)

    kept = assistant._book.sessions_for_task("t1")
    assert len(kept) == 2  # the cap was applied, so something was evicted
    assert good_key in assistant._store.list_keys()  # and it was not the successful run


# -- stopping a firing -----------------------------------------------------------------------------


async def _firing(assistant, prompt="run the report", *, task_name="digest", task_id="t1"):
    """Start a firing that hangs in the model call, and hand back its task plus its conversation id."""
    run = asyncio.create_task(assistant._proactive(prompt, task_name=task_name, task_id=task_id))
    await asyncio.wait_for(assistant._agent.model_client.started.wait(), timeout=2.0)
    running = assistant._tracker.for_task(task_id)
    assert len(running) == 1, "the firing should be tracked while it runs"
    return run, running[0][0]


async def test_a_running_firing_is_tracked_under_its_task_so_it_can_be_stopped(tmp_path):
    """Nothing could cancel a firing before it was tracked: it ran inside the scheduler's job task,
    which only ``Scheduler.cancel`` could reach, and that disarms the task as a side effect."""
    client = _BlockingStreamClient()
    assistant = await Assistant.create(_config(tmp_path), _ConvCapturingChannel(), client_factory=lambda c: client)

    run, conversation_id = await _firing(assistant)
    assert conversation_id != assistant._active_id  # invariant 4: the firing minted its own

    assert assistant.stop_task_runs("t1") == (1, False)
    await asyncio.wait_for(run, timeout=2.0)  # the stop does not escape the run (invariant 6)

    assert assistant._tracker.for_task("t1") == []
    session = assistant._book.get(conversation_id)
    assert any(m.get("content") == "run the report" for m in session.messages)  # partial turn kept
    assert "stopped" in str(session.metadata.get("failure"))


async def test_stopping_a_firing_reports_nothing_as_finished(tmp_path):
    """The announce line tells the user to go read the run's output. A run that was stopped has none,
    so it must not claim to have finished."""
    client = _BlockingStreamClient()
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda c: client)

    run, _ = await _firing(assistant)
    assistant.stop_task_runs("t1")
    await asyncio.wait_for(run, timeout=2.0)

    assert not any("finished" in text for text in channel.sent)


async def test_a_firings_conversation_is_listed_as_running_and_cleared_when_it_ends(tmp_path):
    """What the task panel's Stop button hangs off. The row also has to reach the sidebar at all: the
    firing used to push its conversation only once it had succeeded, so a run in flight was invisible."""
    client = _BlockingStreamClient()
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda c: client)

    run, conversation_id = await _firing(assistant)

    live = [item for item in channel.conversation_pushes[-1] if item["id"] == conversation_id]
    assert live and live[0]["running"] is True and live[0]["task_id"] == "t1"

    assistant.stop_task_runs("t1")
    await asyncio.wait_for(run, timeout=2.0)

    ended = [item for item in channel.conversation_pushes[-1] if item["id"] == conversation_id]
    assert ended and ended[0]["running"] is False


async def test_a_firing_is_never_stopped_from_inside_itself(tmp_path):
    """A task whose own prompt leads the model to stop it would otherwise cut its turn off mid-tool-call,
    leaving a transcript that reads like a crash and no room to say why."""
    from kokua.channels.web import streaming_conversation

    client = _BlockingStreamClient()
    assistant = await Assistant.create(_config(tmp_path), _ConvCapturingChannel(), client_factory=lambda c: client)

    run, conversation_id = await _firing(assistant)
    token = streaming_conversation.set(conversation_id)  # as the run's own tool call would see it
    try:
        assert assistant.stop_task_runs("t1") == (0, True)
    finally:
        streaming_conversation.reset(token)

    assert assistant._tracker.for_task("t1"), "the run it was called from is still going"
    assert assistant.stop_task_runs("t1") == (1, False)  # from outside, it stops
    await asyncio.wait_for(run, timeout=2.0)


async def test_shutdown_cancellation_still_takes_the_firing_down_with_it(tmp_path):
    """A firing runs in a child task now, so the two cancellations have to stay distinguishable: a stop
    ends the run and lets the scheduler job carry on, while a cancellation aimed at the job itself
    (shutdown) has to keep propagating -- and must not leave the child running behind it."""
    client = _BlockingStreamClient()
    assistant = await Assistant.create(_config(tmp_path), _ConvCapturingChannel(), client_factory=lambda c: client)

    run, _ = await _firing(assistant)
    child = assistant._tracker.for_task("t1")[0][1].handle

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    await asyncio.gather(child.task, return_exceptions=True)

    assert child.done


async def test_stop_reaches_a_firing_once_you_have_switched_into_its_conversation(tmp_path):
    """The surface that comes free from tracking the run: the firing's conversation now reports a turn in
    flight, so switching into it shows the working indicator, and `/stop` reaches the firing itself."""
    client = _BlockingStreamClient()
    assistant = await Assistant.create(_config(tmp_path), _ConvCapturingChannel(), client_factory=lambda c: client)

    run, conversation_id = await _firing(assistant)
    await assistant.select_conversation(conversation_id)
    assert assistant.turn_running(conversation_id) is True

    assistant._stop_active_turn()  # the helper the `/stop` branch calls
    await asyncio.wait_for(run, timeout=2.0)

    assert assistant._tracker.for_task("t1") == []
    assert "stopped" in str(assistant._book.get(conversation_id).metadata.get("failure"))


async def test_a_stopped_firing_says_so_where_the_user_can_see_it(tmp_path):
    """On a channel with no conversation list a firing runs in the conversation being viewed, so the stop
    belongs in that conversation the way a reactive turn's does, not only in metadata."""
    client = _BlockingStreamClient()
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    run = asyncio.create_task(assistant._proactive("run it", task_name="digest", task_id="t1"))
    await asyncio.wait_for(client.started.wait(), timeout=2.0)
    assert assistant.stop_task_runs("t1") == (1, False)
    await asyncio.wait_for(run, timeout=2.0)

    assert "(stopped)" in channel.sent


async def test_shutdown_waits_for_a_turn_a_later_message_displaced(tmp_path):
    """Shutdown closes the session store, so no turn may still be running when it does.

    Two messages arriving back to back on one conversation each start a task, and the tracker holds one
    entry per conversation, so the second submission replaces the first's. That replacement is
    deliberate (see invariant 7), but it means the tracker is not the list of turns still in flight, and
    shutdown was reading it as if it were: the displaced turn was neither cancelled nor awaited, so the
    event loop cancelled it after the store had closed, and the provenance record every cancelled turn
    makes on its way down raised `I/O operation on closed file` out of a task nobody was watching.
    """
    client = _BlockingStreamClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(["first", "second"]), client=client)

    await asyncio.wait_for(assistant.run(), timeout=5.0)

    turns = [task for task in asyncio.all_tasks() if "Assistant._handle" in repr(task.get_coro())]
    assert not turns, f"{len(turns)} turn task(s) outlived the store: {turns}"


async def test_shutdown_cancels_a_pending_title_rather_than_waiting_on_the_endpoint(tmp_path, monkeypatch):
    """A generated title is disposable, and the call behind it is a model request.

    So shutdown cancels it instead of awaiting it: an unresponsive endpoint would otherwise hold the
    process open long after its last turn. What it must not do is leave the task running past
    ``store.close()``, which is invariant 7's failure (a write to a closed file, out of a task nobody
    is watching) reached by a task that is not a turn.
    """
    never = asyncio.Event()

    async def never_answers(model, first_message):
        await never.wait()
        return "too late"

    monkeypatch.setattr("kokua.core.titles.summarize_title", never_answers)
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(["plan my trip"]), client=MockAsyncModelClient(["ok"])
    )

    await asyncio.wait_for(assistant.run(), timeout=5.0)

    pending = [task for task in asyncio.all_tasks() if "_write_title" in repr(task.get_coro())]
    assert not pending, f"{len(pending)} title task(s) outlived the store: {pending}"
