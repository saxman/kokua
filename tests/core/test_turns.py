"""TurnRunner: reactive turns, proactive (scheduled) turns, cancellation, and concurrency."""

from __future__ import annotations

import asyncio


from aimu.aio.channels.base import Channel, ChannelMessage

from kokua.core.assistant import Assistant
from tests.channels import FakeChannel, _ConvCapturingChannel, _config
from tests.fakes import _BlockingStreamClient, _RequestsToolOnce
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


async def test_proactive_auto_denies_gated_tool_on_viewed_conversation(tmp_path):
    """A target="active" proactive run auto-denies a gated tool even when it fires on the CURRENTLY
    VIEWED conversation (where streaming_conversation == _active_id would otherwise look foreground and
    wrongly prompt). Unattended turns must never prompt."""
    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    client = _RequestsToolOnce("execute_python", {"code": "print(1)"})
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)

    # No streaming_conversation is set by the test; _proactive sets it to _active_id (the viewed
    # conversation) itself, so only the proactive marker keeps this from prompting.
    await asyncio.wait_for(assistant._proactive("do it"), timeout=2.0)

    denied = [m for m in client.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'execute_python' was not approved."


async def test_proactive_new_session_auto_denies_gated_tool(tmp_path):
    """The target="new" path (fresh conversation, never the viewed one) also auto-denies."""
    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    client = _RequestsToolOnce("execute_python", {"code": "print(1)"})
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: client)

    await asyncio.wait_for(assistant._proactive("do it", target="new", task_name="t"), timeout=2.0)

    denied = [m for m in client.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'execute_python' was not approved."


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
    """Yields a normal message, waits until the turn is running, then yields '/stop'."""

    name = "fake"

    def __init__(self, started):
        self._started = started
        self.sent: list[str] = []

    async def receive(self):
        yield ChannelMessage(text="long task", channel="fake")
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
    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    assistant = await Assistant.create(
        cfg, channel, client_factory=lambda cid: _RecordingRequestsToolOnce("execute_python", {"code": "1+1"})
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
    assert denied and denied[-1]["content"] == "Tool 'execute_python' was not approved."

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


async def test_a_proactive_turn_records_its_subagent_events(tmp_path):
    """A scheduled task's delegation is recorded against the message index the run started at."""
    client = _SpawningClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    client.reporter = assistant._subagent_reporter

    await assistant._proactive("delegate this")

    metadata = assistant._store.get(assistant._active_id).metadata
    assert [event["id"] for event in metadata["subagent"]["0"]] == ["r-1", "r-1"]
