"""TurnRunner: reactive turns, proactive (scheduled) turns, cancellation, and concurrency."""

from __future__ import annotations

import asyncio

import pytest

from aimu.aio.channels.base import Channel, ChannelMessage

from kokua.core.assistant import Assistant
from kokua.workflows import Workflow
from tests.channels import FakeChannel, _ConvCapturingChannel, _config
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
    """A target="active" proactive run auto-denies a gated tool even when it fires on the CURRENTLY
    VIEWED conversation (where streaming_conversation == _active_id would otherwise look foreground and
    wrongly prompt). Unattended turns must never prompt."""
    cfg = _config(tmp_path, confirm_tools=["update_config"])
    client = _RequestsToolOnce("update_config", {"section": "display", "key": "show_tools", "value": "false"})
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)

    # No streaming_conversation is set by the test; _proactive sets it to _active_id (the viewed
    # conversation) itself, so only the proactive marker keeps this from prompting.
    await asyncio.wait_for(assistant._proactive("do it"), timeout=2.0)

    denied = [m for m in client.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'update_config' was not approved."


async def test_proactive_new_session_auto_denies_gated_tool(tmp_path):
    """The target="new" path (fresh conversation, never the viewed one) also auto-denies."""
    cfg = _config(tmp_path, confirm_tools=["update_config"])
    client = _RequestsToolOnce("update_config", {"section": "display", "key": "show_tools", "value": "false"})
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: client)

    await asyncio.wait_for(assistant._proactive("do it", target="new", task_name="t"), timeout=2.0)

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

    await assistant._proactive("run the report", target="new", task_name="report")

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
    """Every conversation a task mints records which task minted it, for both "new" and "task"
    targets. A "new" task has no session_id to link by, so without this stamp its firings would be
    indistinguishable from conversations the user started."""
    assistant = await Assistant.create(
        _config(tmp_path), _ConvCapturingChannel(), client_factory=lambda cid: MockAsyncModelClient(["out"])
    )

    new_key = await assistant._proactive("run it", target="new", task_name="report", task_id="task-1")
    task_key = await assistant._proactive("run it", target="task", task_name="digest", task_id="task-2")

    assert assistant._store.get(new_key).metadata["task_id"] == "task-1"
    assert assistant._store.get(task_key).metadata["task_id"] == "task-2"


async def test_proactive_active_target_stamps_nothing(tmp_path):
    """A target="active" firing runs in the conversation the user is already viewing, so it must not
    claim that conversation for the task."""
    assistant = await Assistant.create(
        _config(tmp_path), _ConvCapturingChannel(), client_factory=lambda cid: MockAsyncModelClient(["out"])
    )

    await assistant._proactive("run it", target="active", task_name="report", task_id="task-1")

    assert "task_id" not in assistant._store.get(assistant._active_id).metadata


async def test_proactive_new_session_degrades_on_single_conversation_channel(tmp_path):
    channel = FakeChannel()  # no send_conversations
    client = MockAsyncModelClient(["task output"])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)
    active_key = assistant._session.key

    await assistant._proactive("run the report", target="new", task_name="report")

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

    await assistant._proactive("run the report", target="new", task_name="report")

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
            "update_config", {"section": "display", "key": "show_tools", "value": "false"}
        ),
    )
    viewed = assistant._active_id

    await assistant._proactive("run the report", target="new", task_name="report")

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


async def test_proactive_task_target_reuses_created_conversation(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out1", "out2"])
    )
    active_key = assistant._active_id

    first_key = await assistant._proactive("first run", target="task", task_name="digest")
    assert first_key is not None and first_key != active_key
    keys_after_first = set(assistant._store.list_keys())

    second_key = await assistant._proactive("second run", target="task", task_name="digest", session_id=first_key)

    assert second_key == first_key  # reused, not a fresh conversation
    assert set(assistant._store.list_keys()) == keys_after_first  # no new conversation created
    contents = [m.get("content") for m in assistant._store.get(first_key).messages]
    assert "out1" in contents and "out2" in contents  # both firings' replies accumulate
    assert "first run" in contents and "second run" in contents
    assert assistant._active_id == active_key  # viewed conversation untouched


async def test_proactive_task_target_recreates_when_conversation_deleted(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out1", "out2"])
    )
    first_key = await assistant._proactive("first", target="task", task_name="digest")
    assistant._store.delete(first_key)
    assistant._registry.discard(first_key)

    second_key = await assistant._proactive("second", target="task", task_name="digest", session_id=first_key)

    assert second_key and second_key != first_key  # stale id not resurrected as an empty session
    assert first_key not in assistant._store.list_keys()
    assert second_key in assistant._store.list_keys()


async def test_proactive_latest_target_replaces_the_conversation_it_remembers(tmp_path):
    """The point of "latest": a task that fires often keeps its most recent run and nothing older,
    without the user deleting the pile by hand. Unlike "task" it never writes into the remembered
    conversation -- it mints a fresh one and drops the old."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out1", "out2"])
    )
    active_key = assistant._active_id

    first_key = await assistant._proactive("first run", target="latest", task_name="digest")
    assert first_key is not None and first_key != active_key
    assert first_key in assistant._store.list_keys()  # nothing to replace on the first firing

    second_key = await assistant._proactive("second", target="latest", task_name="digest", session_id=first_key)

    assert second_key and second_key != first_key  # a fresh conversation, not the remembered one
    assert first_key not in assistant._store.list_keys()  # ...and the one it replaced is gone
    assert second_key in assistant._store.list_keys()
    assert active_key in assistant._store.list_keys()  # the user's own conversation is untouched


async def test_proactive_latest_target_keeps_the_previous_run_when_the_new_one_fails(tmp_path):
    """Deleting only after the run succeeds is what keeps a failed firing from leaving nothing to
    read: the last good run has to outlive a bad one."""
    channel = _ConvCapturingChannel()
    clients = iter([MockAsyncModelClient(["out1"]), _FailingClient([])])
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda cid: next(clients))

    first_key = await assistant._proactive("first run", target="latest", task_name="digest")
    await assistant._proactive("second", target="latest", task_name="digest", session_id=first_key)

    assert first_key in assistant._store.list_keys()  # the last good run survived the failure


async def test_proactive_latest_target_tolerates_an_already_deleted_conversation(tmp_path):
    """The user can delete a run themselves between firings, so the replace must not be the thing
    that takes down the scheduler job (invariant 6)."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client_factory=lambda cid: MockAsyncModelClient(["out1", "out2"])
    )
    first_key = await assistant._proactive("first", target="latest", task_name="digest")
    await assistant.delete_conversation(first_key)

    second_key = await assistant._proactive("second", target="latest", task_name="digest", session_id=first_key)

    assert second_key and second_key in assistant._store.list_keys()


class _FailingClient(MockAsyncModelClient):
    """Raises on run, standing in for an unreachable or misconfigured model server."""

    async def chat(self, *args, **kwargs):
        raise RuntimeError("model exploded")

    async def chat_streamed(self, *args, **kwargs):
        raise RuntimeError("model exploded")


async def test_proactive_task_target_surfaces_errors_and_still_returns_its_key(tmp_path):
    """A failing scheduled firing must not escape into the scheduler job, which has no handler.

    Before the proactive paths were unified, only target="active" had error handling: the
    "new"/"task" branch returned early, past _proactive's handlers, so a model error propagated into
    scheduling._fire_job and would take the scheduler down. It also skipped _remember_session, so a
    task whose first firing failed forgot the conversation it had just minted and created another one
    on every later firing.
    """
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client_factory=lambda cid: _FailingClient([]))
    active_key = assistant._active_id

    key = await assistant._proactive("do it", target="task", task_name="digest")

    assert key is not None and key != active_key  # the key is returned despite the failure...
    assert key in assistant._store.list_keys()  # ...and names a real conversation to reuse
    assert any("failed" in str(text) for text in channel.sent)  # the user is told
    assert assistant._active_id == active_key  # the viewed conversation is untouched

    # The next firing reuses that conversation instead of minting a second one.
    before = set(assistant._store.list_keys())
    again = await assistant._proactive("do it", target="task", task_name="digest", session_id=key)
    assert again == key
    assert set(assistant._store.list_keys()) == before


async def test_proactive_active_target_surfaces_errors(tmp_path):
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
    recording, per `record_subagent_events`). This is the coverage `test_a_stopped_planned_turn_...`
    used to give invariant 5 for the planning workflow specifically; that test now hangs (planning has
    no workflow toolset yet), so this one covers the shape itself, independent of any one workflow."""
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
    would be. The plan branch used to take its index from the returned PlanResult, which a cancelled
    run never produces, so the index stayed -1 and the recording silently no-opped."""
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
    assistant = await Assistant.create(_config(tmp_path, show_reasoning=True), channel, client=client)
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
    assistant = await Assistant.create(_config(tmp_path, plan_review=True), FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter
    active_id = assistant._active_id

    turn = asyncio.create_task(
        assistant._handle(ChannelMessage(text="do X", channel="fake"), conversation_id=active_id, plan=True)
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
    record call. Before the fix, `_record_subagents` ran after that send, so this second
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
# That is the position conversation_to_frames looks a turn's cards up by, so anything else drops them.


def _user_positions(messages: list[dict]) -> list[int]:
    """The real positions of the user messages, as ``conversation_to_frames`` enumerates them."""
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

    await assistant._handle(ChannelMessage(text="do X", channel="fake"), conversation_id=active_id, plan=True)

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

    await assistant._proactive("run the report", target="new", task_name="report")

    task_id = next(key for key in assistant._store.list_keys() if key != active_id)
    assert ("begin", task_id, "run the report") in channel.calls
    # Ended at the persist, so a switch-in landing after it replays the store alone, not both.
    assert channel.calls.index(("end", task_id)) > channel.calls.index(("begin", task_id, "run the report"))
