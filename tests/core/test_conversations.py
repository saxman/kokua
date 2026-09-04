"""ConversationBook: the store, the active pointer, and switching between conversations."""

from __future__ import annotations


import asyncio

import pytest

from aimu.aio.channels.base import ChannelMessage
from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_KEY

from kokua.core.assistant import Assistant
from kokua.core.conversations import ConversationNotFound, TurnInFlight, TurnNotFound
from tests.channels import FakeChannel, _ConvCapturingChannel, _config
from tests.helpers import BlockingModelClient, MockAsyncModelClient, settle_titles


def _fixed_title(title: str):
    """A title writer that answers *title* whatever it is asked."""

    async def write(model, first_message):
        return title

    return write


async def test_turn_persists_to_active_session_with_title(tmp_path):
    channel = FakeChannel()
    client = MockAsyncModelClient(["Sure."])
    assistant = await Assistant.create(_config(tmp_path), channel, client=client)

    await assistant._handle(
        ChannelMessage(text="plan my trip to Kauai", channel="fake"), conversation_id=assistant._active_id
    )

    stored = assistant._store.get(assistant._session.key)
    assert any(m.get("content") == "plan my trip to Kauai" for m in stored.messages)
    assert stored.metadata["title"] == "plan my trip to Kauai"


async def test_history_returns_active_session_messages(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))
    await assistant._handle(ChannelMessage(text="hello", channel="fake"), conversation_id=assistant._active_id)
    assert assistant.history == assistant._session.messages
    assert any(m.get("content") == "hello" for m in assistant.history)


async def test_fresh_start_has_empty_active_session(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant._session.messages == []
    assert assistant._store.list_keys() == [assistant._session.key]


async def test_first_turn_pushes_conversations(tmp_path):
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["a", "b"]))

    await assistant._handle(ChannelMessage(text="hello there", channel="fake"), conversation_id=assistant._active_id)
    assert len(channel.conversation_pushes) == 1  # title just set -> one push
    assert channel.conversation_pushes[0][0]["title"] == "hello there"

    await assistant._handle(ChannelMessage(text="again", channel="fake"), conversation_id=assistant._active_id)
    assert len(channel.conversation_pushes) == 1  # title already set -> no further push


async def test_list_conversations_recency_desc(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="first chat", channel="fake"), conversation_id=assistant._active_id)
    first_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="second chat", channel="fake"), conversation_id=assistant._active_id)
    second_id = assistant._session.key

    items = assistant.list_conversations()
    assert [i["id"] for i in items] == [second_id, first_id]  # most recent first
    assert items[0]["title"] == "second chat"
    assert items[0]["active"] is True and items[1]["active"] is False


async def test_new_conversation_resets_agent(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="old chat", channel="fake"), conversation_id=assistant._active_id)
    assert assistant._agent.model_client.messages  # has the old turn

    new_id = await assistant.new_conversation()
    assert assistant._session.key == new_id
    assert assistant._session.messages == []
    assert assistant._agent.model_client.messages == []  # the new conversation's agent is freshly built


async def test_select_conversation_restores_messages(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="keep me", channel="fake"), conversation_id=assistant._active_id)
    first_id = assistant._session.key
    await assistant.new_conversation()
    assert not any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)

    await assistant.select_conversation(first_id)
    assert assistant._session.key == first_id
    assert any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)


async def test_delete_inactive_conversation_leaves_active(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="old chat", channel="fake"), conversation_id=assistant._active_id)
    old_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="current chat", channel="fake"), conversation_id=assistant._active_id)
    active_id = assistant._session.key

    await assistant.delete_conversation(old_id)
    assert old_id not in assistant._store.list_keys()
    assert assistant._session.key == active_id  # active unchanged
    assert any(m.get("content") == "current chat" for m in assistant._agent.model_client.messages)


async def test_delete_active_switches_to_most_recent_remaining(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="keep me", channel="fake"), conversation_id=assistant._active_id)
    keep_id = assistant._session.key
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="delete me", channel="fake"), conversation_id=assistant._active_id)
    delete_id = assistant._session.key

    await assistant.delete_conversation(delete_id)
    assert delete_id not in assistant._store.list_keys()
    assert assistant._session.key == keep_id  # switched to the remaining one
    assert any(m.get("content") == "keep me" for m in assistant._agent.model_client.messages)


async def test_delete_last_conversation_creates_fresh_empty(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="only chat", channel="fake"), conversation_id=assistant._active_id)
    only_id = assistant._session.key

    await assistant.delete_conversation(only_id)
    assert only_id not in assistant._store.list_keys()
    assert assistant._session.key != only_id  # a fresh, empty active conversation
    assert assistant._session.messages == []
    assert assistant._agent.model_client.messages == []


async def test_registry_used_for_active_conversation(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    agent = assistant._registry.get(assistant._active_id)
    assert assistant._agent is agent  # the _agent property resolves to the active conversation's agent


async def test_switch_conversation_isolates_message_lists(tmp_path):
    cfg = _config(tmp_path)
    factory = lambda cid: MockAsyncModelClient(["reply in c1"])  # noqa: E731
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=factory)
    first_id = assistant._active_id
    # Run a turn in the first conversation.
    await assistant._handle(ChannelMessage(text="hello", sender="t", channel="t"), conversation_id=assistant._active_id)
    second_id = await assistant.new_conversation()
    assert second_id != first_id
    # The new conversation's agent has its own (empty) message list.
    assert assistant._agent.model_client.messages == []
    # Switching back does not replay onto the wrong agent.
    await assistant.select_conversation(first_id)
    assert any("reply in c1" == m.get("content") for m in assistant._agent.model_client.messages)


async def test_handle_persists_to_its_own_conversation_not_active(tmp_path):
    # A turn bound to conversation A must persist to A even if _active_id moved to B mid-flight.
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["reply-A"]))
    conv_a = assistant._active_id
    conv_b = await assistant.new_conversation()  # _active_id now B
    assert assistant._active_id == conv_b
    await assistant._handle(ChannelMessage(text="hi", sender="t", channel="t"), conversation_id=conv_a)
    stored_a = assistant._store.get(conv_a)
    assert any(m.get("content") == "reply-A" for m in stored_a.messages)
    stored_b = assistant._store.get(conv_b)
    assert not any(m.get("content") == "reply-A" for m in stored_b.messages)


async def test_select_conversation_reverts_active_id_on_build_failure(tmp_path):
    from kokua.core.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial conversation builds fine; the selected one fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    original_id = assistant._active_id

    with pytest.raises(ModelClientError):
        await assistant.select_conversation("does-not-exist-yet")

    # The failed build must not leave the assistant pointed at an unbuildable conversation.
    assert assistant._active_id == original_id


async def test_new_conversation_reverts_active_id_on_build_failure(tmp_path):
    from kokua.core.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial conversation builds fine; the new one fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    original_id = assistant._active_id

    with pytest.raises(ModelClientError):
        await assistant.new_conversation()

    # The failed build must not leave the assistant pointed at the unbuildable new conversation;
    # it reverts to the one that was active before the call (its session record still lingers in
    # the store, unused but harmless).
    assert assistant._active_id == original_id


async def test_delete_conversation_reverts_active_id_to_deleted_id_on_build_failure(tmp_path):
    """delete_conversation's revert is documented as best-effort: the deleted conversation's store
    record and registry entry are already gone by the time the replacement's build fails, so
    reverting only restores the id, not a working conversation. This asserts that documented
    behavior (not a full rollback, which would need deferring the delete itself)."""
    from kokua.core.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the initial (soon-to-be-deleted) conversation builds fine; its
            # replacement (a fresh empty conversation, since none remain) fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    original_id = assistant._active_id

    with pytest.raises(ModelClientError):
        await assistant.delete_conversation(original_id)

    # Reverts to the just-deleted id (documented best-effort semantics), not to some other,
    # never-vetted conversation.
    assert assistant._active_id == original_id
    # The delete itself was not rolled back: the store no longer has a record for that id
    # (TinyDBSessionStore.get returns a fresh, unsaved Session for a missing key, not None).
    assert original_id not in assistant._store.list_keys()


async def test_persist_writes_active_conversation(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(
        cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["hi there"])
    )
    active = assistant._active_id
    await assistant._handle(ChannelMessage(text="hello", sender="t", channel="t"), conversation_id=assistant._active_id)
    reloaded = assistant._store.get(active)
    assert any(m.get("content") == "hi there" for m in reloaded.messages)


async def test_switch_methods_sync_channel_active_conversation_id(tmp_path):
    """Item 1 from the Task 5 review: the muting key (WebChannel.active_conversation_id) and the
    background-completion notification key (Assistant._active_id) must agree on what's viewed, so
    every switch method mirrors _active_id onto the channel (when it tracks one)."""

    class _TrackingChannel(FakeChannel):
        def __init__(self):
            super().__init__()
            self.active_conversation_id = None

    channel = _TrackingChannel()
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))

    new_id = await assistant.new_conversation()
    assert channel.active_conversation_id == new_id == assistant._active_id

    other_id = await assistant.new_conversation()
    assert channel.active_conversation_id == other_id == assistant._active_id

    await assistant.select_conversation(new_id)
    assert channel.active_conversation_id == new_id == assistant._active_id

    await assistant.delete_conversation(other_id)
    assert channel.active_conversation_id == assistant._active_id  # unaffected: not the deleted one

    await assistant.delete_conversation(new_id)  # deletes the viewed one -> falls back to a fresh one
    assert channel.active_conversation_id == assistant._active_id


async def test_recording_subagent_events_extends_rather_than_replaces(tmp_path):
    """A planned turn can both review a plan and spawn sub-agents; neither may erase the other."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    conversation_id = assistant._active_id

    assistant._book.record_turn_provenance([{"id": "a", "status": "done"}], "", 0, conversation_id)
    assistant._book.record_turn_provenance([{"id": "b", "status": "done"}], "", 0, conversation_id)

    stored = assistant._store.get(conversation_id).metadata["subagent"]["0"]
    assert [event["id"] for event in stored] == ["a", "b"]


async def test_recording_a_turn_that_produced_nothing_writes_no_metadata(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assistant._book.record_turn_provenance([], "", 0, assistant._active_id)

    metadata = assistant._store.get(assistant._active_id).metadata
    assert "subagent" not in metadata and "model" not in metadata


async def test_the_turn_model_is_recorded_even_when_no_subagent_ran(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assistant._book.record_turn_provenance([], "ollama:qwen3:8b", 0, assistant._active_id)

    metadata = assistant._store.get(assistant._active_id).metadata
    assert metadata["model"] == {"0": "ollama:qwen3:8b"}
    assert "subagent" not in metadata


async def test_new_session_records_the_task_that_minted_it(tmp_path):
    """A scheduled task's conversation carries the task id, which is what lets the sidebar nest it
    under its task. Names are optional and editable, so the id is the only durable link."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a"]))

    session = assistant._book.new_session(title="morning-brief", task_id="abc123")

    assert assistant._store.get(session.key).metadata["task_id"] == "abc123"


async def test_list_projects_task_id_and_leaves_it_none_for_a_chat(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a"]))
    task_session = assistant._book.new_session(title="morning-brief", task_id="abc123")

    by_id = {item["id"]: item for item in assistant._book.list()}

    assert by_id[task_session.key]["task_id"] == "abc123"
    assert by_id[assistant._active_id]["task_id"] is None


async def test_sessions_backs_list_and_shares_its_ordering(tmp_path):
    """``sessions()`` is the single store-walk-and-order seam; ``list()`` is a projection of it, so the
    two can never disagree about recency."""
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["a"])
    )
    await assistant._handle(ChannelMessage(text="first chat", channel="fake"), conversation_id=assistant._active_id)
    await assistant.new_conversation()
    await assistant._handle(ChannelMessage(text="second chat", channel="fake"), conversation_id=assistant._active_id)

    book = assistant._book
    assert [session.key for session in book.sessions()] == [item["id"] for item in book.list()]
    assert len(book.sessions()) == 2


def test_resolve_accepts_a_unique_prefix_but_not_an_ambiguous_or_short_one(tmp_path):
    """A caller that saw a 32-hex id in a listing tends to shorten it, so a long-enough unique fragment
    resolves; an ambiguous one must not, or it would silently open the wrong conversation."""
    import asyncio

    from aimu.sessions import Session, TinyDBSessionStore

    from kokua.core.conversations import ConversationBook
    from kokua.core.turn_gate import TurnGate

    config = _config(tmp_path)
    store = TinyDBSessionStore(str(config.sessions_path))
    for key in ("abcdef0000", "abcdef1111", "zzzzzz9999"):
        store.save(Session(key=key, metadata={"updated_at": "2026-08-11T09:14:00"}, messages=[]))
    book = ConversationBook(store, TurnGate(lambda cid: asyncio.Lock()), config, on_active_change=lambda _cid: None)

    assert book.resolve("zzzzzz9999").key == "zzzzzz9999"
    assert book.resolve("zzzzzz").key == "zzzzzz9999"
    assert book.resolve("abcdef") is None  # ambiguous
    assert book.resolve("zzz") is None  # shorter than ID_PREFIX_MIN
    assert book.resolve("") is None


async def test_sessions_for_task_returns_only_that_tasks_conversations_oldest_first(tmp_path):
    """Retention prunes a task's own conversations in the order they were minted, so the book owns
    both the filter and the ``created_at`` ordering rather than each caller re-deriving them."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    book = assistant._book

    first = book.new_session(title="run 1", task_id="task-1")
    second = book.new_session(title="run 2", task_id="task-1")
    book.new_session(title="elsewhere", task_id="task-2")
    book.new_session(title="mine")
    first.metadata["created_at"] = "2026-01-01T00:00:00"
    second.metadata["created_at"] = "2026-01-02T00:00:00"
    book.save(first)
    book.save(second)

    # A later turn on the older conversation must not reorder it: minting order is what counts.
    book.touch(first)

    assert [s.key for s in book.sessions_for_task("task-1")] == [first.key, second.key]


async def test_the_turn_thinking_is_recorded_alongside_the_model(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assistant._book.record_turn_provenance([], "ollama:qwen3:8b", 0, assistant._active_id, thinking="high")

    metadata = assistant._store.get(assistant._active_id).metadata
    assert metadata["thinking"] == {"0": "high"}


async def test_record_turn_provenance_persists_usage_under_the_turn_index(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assistant._book.record_turn_provenance(
        [], "some-model", 2, assistant._active_id, usage={"calls": 3, "input_tokens": 90}
    )

    stored = assistant._store.get(assistant._active_id).metadata["usage"]["2"]
    assert stored == {"calls": 3, "input_tokens": 90}


async def test_record_turn_provenance_writes_nothing_when_there_is_nothing_to_write(tmp_path):
    """Usage joins the other guards: a call carrying none of them must not touch the file."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assistant._book.record_turn_provenance([], "", -1, assistant._active_id, usage=None)

    assert "usage" not in assistant._store.get(assistant._active_id).metadata


async def test_a_turn_that_reasoned_at_no_configured_effort_records_no_thinking(tmp_path):
    """``None`` is the common case (AIMU's own default), so recording it would bloat every session file."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assistant._book.record_turn_provenance([], "ollama:qwen3:8b", 0, assistant._active_id, thinking=None)

    assert "thinking" not in assistant._store.get(assistant._active_id).metadata


async def test_a_turn_with_reasoning_off_records_that_rather_than_nothing(tmp_path):
    """``False`` is a declaration, so the guard cannot be a truthiness test or it would read as unset."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))

    assistant._book.record_turn_provenance([], "", 0, assistant._active_id, thinking=False)

    assert assistant._store.get(assistant._active_id).metadata["thinking"] == {"0": False}


async def test_retag_task_repoints_every_conversation_the_task_minted(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    book = assistant._book

    book.new_session(title="run 1", task_id="morning-brief")
    book.new_session(title="run 2", task_id="morning-brief")
    book.new_session(title="unrelated")

    moved = book.retag_task("morning-brief", "daily-brief")

    assert moved == 2
    assert book.sessions_for_task("morning-brief") == []
    assert len(book.sessions_for_task("daily-brief")) == 2


async def test_retag_task_is_zero_when_the_task_minted_nothing(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    book = assistant._book

    assert book.retag_task("never-ran", "renamed") == 0


async def test_delete_does_not_wait_for_a_turn_on_another_conversation(tmp_path):
    """Deleting one conversation waits for that conversation, not for every conversation.

    ``delete`` took the gate's exclusive (writer) hold, which drains *every* in-flight turn before it
    proceeds. A delete therefore blocked for as long as an unrelated conversation's turn ran, and since
    the web front end's socket reader awaits the delete inline, one long turn wedged the whole UI.
    """
    clients: dict = {}

    def factory(conversation_id):
        client = BlockingModelClient() if not clients else MockAsyncModelClient(["ok"])
        clients[conversation_id] = client
        return client

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    busy_id = assistant._active_id
    blocking = clients[busy_id]
    await assistant.new_conversation()
    doomed_id = assistant._active_id
    await assistant.new_conversation()  # somewhere to stand that is neither busy nor doomed

    turn = asyncio.create_task(
        assistant._handle(ChannelMessage(text="a long job", channel="fake"), conversation_id=busy_id)
    )
    await asyncio.wait_for(blocking.started.wait(), timeout=5)

    try:
        await asyncio.wait_for(assistant.delete_conversation(doomed_id), timeout=5)
    finally:
        blocking.release.set()
        await turn

    assert doomed_id not in assistant._store.list_keys()


async def test_retitle_replaces_the_placeholder_it_was_given(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))
    await assistant._handle(ChannelMessage(text="plan a trip", channel="fake"), conversation_id=assistant._active_id)
    conversation_id = assistant._active_id

    wrote = await assistant._book.retitle(conversation_id, "Kauai trip planning", replacing="plan a trip")

    assert wrote is True
    stored = assistant._store.get(conversation_id)
    assert stored.metadata["title"] == "Kauai trip planning"
    assert any(m.get("content") == "plan a trip" for m in stored.messages)


async def test_retitle_leaves_a_title_that_changed_underneath(tmp_path):
    """Whatever is in the slot now wins. Nothing renames a conversation today; this is the guard
    standing ahead of anything that does, and the same one that keeps a deleted conversation dead."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))
    await assistant._handle(ChannelMessage(text="plan a trip", channel="fake"), conversation_id=assistant._active_id)
    conversation_id = assistant._active_id
    renamed = assistant._store.get(conversation_id)
    renamed.metadata["title"] = "Kauai, take two"
    assistant._store.save(renamed)

    wrote = await assistant._book.retitle(conversation_id, "Kauai trip planning", replacing="plan a trip")

    assert wrote is False
    assert assistant._store.get(conversation_id).metadata["title"] == "Kauai, take two"


async def test_retitle_does_not_resurrect_a_deleted_conversation(tmp_path):
    """``store.get`` answers a missing key with a fresh empty session, so a blind write would
    re-create the row the delete removed, holding nothing but a title."""
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["ok"])
    )
    await assistant._handle(ChannelMessage(text="plan a trip", channel="fake"), conversation_id=assistant._active_id)
    doomed_id = assistant._active_id
    await assistant.new_conversation()
    await assistant.delete_conversation(doomed_id)

    wrote = await assistant._book.retitle(doomed_id, "Kauai trip planning", replacing="plan a trip")

    assert wrote is False
    assert doomed_id not in assistant._store.list_keys()


async def test_retitle_waits_for_a_turn_running_on_its_conversation(tmp_path):
    """The store saves whole sessions, so an unsynchronized read-modify-write would revert whatever
    the running turn persists between the read and the write. Held under the conversation's own turn
    slot, like ``delete``."""
    clients: dict = {}

    def factory(conversation_id):
        client = BlockingModelClient() if not clients else MockAsyncModelClient(["ok"])
        clients[conversation_id] = client
        return client

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    busy_id = assistant._active_id
    blocking = clients[busy_id]

    turn = asyncio.create_task(
        assistant._handle(ChannelMessage(text="a long job", channel="fake"), conversation_id=busy_id)
    )
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    retitle = asyncio.create_task(assistant._book.retitle(busy_id, "A long job", replacing="a long job"))
    await asyncio.sleep(0)

    try:
        assert not retitle.done(), "retitle wrote while a turn held the conversation"
    finally:
        blocking.release.set()
        await turn

    assert await asyncio.wait_for(retitle, timeout=5) is True
    stored = assistant._store.get(busy_id)
    assert stored.metadata["title"] == "A long job"
    assert any(m.get("role") == "assistant" for m in stored.messages), "the turn's reply was reverted"


async def test_a_first_turn_upgrades_its_derived_title_with_a_generated_one(tmp_path, monkeypatch):
    async def title(model, first_message):
        assert first_message == "plan my trip to Kauai"
        return "Kauai trip planning"

    monkeypatch.setattr("kokua.core.titles.summarize_title", title)
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))

    await assistant._handle(
        ChannelMessage(text="plan my trip to Kauai", channel="fake"), conversation_id=assistant._active_id
    )
    assert assistant._store.get(assistant._active_id).metadata["title"] == "plan my trip to Kauai"

    await settle_titles(assistant)
    assert assistant._store.get(assistant._active_id).metadata["title"] == "Kauai trip planning"


async def test_a_generated_title_pushes_the_conversation_list_again(tmp_path, monkeypatch):
    monkeypatch.setattr("kokua.core.titles.summarize_title", _fixed_title("Kauai trip planning"))
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["ok"]))

    await assistant._handle(ChannelMessage(text="plan my trip", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)

    assert [push[0]["title"] for push in channel.conversation_pushes] == ["plan my trip", "Kauai trip planning"]


async def test_a_title_the_model_would_not_write_leaves_the_placeholder(tmp_path):
    """The autouse stub answers None, which is what a down endpoint answers."""
    channel = _ConvCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient(["ok"]))

    await assistant._handle(ChannelMessage(text="plan my trip", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)

    assert assistant._store.get(assistant._active_id).metadata["title"] == "plan my trip"
    assert len(channel.conversation_pushes) == 1


async def test_a_second_turn_writes_no_further_title(tmp_path, monkeypatch):
    calls: list = []

    async def title(model, first_message):
        calls.append(first_message)
        return "Kauai trip planning"

    monkeypatch.setattr("kokua.core.titles.summarize_title", title)
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["a", "b"]))

    await assistant._handle(ChannelMessage(text="plan my trip", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)
    await assistant._handle(ChannelMessage(text="and my week", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)

    assert calls == ["plan my trip"]


async def test_a_task_conversation_keeps_the_task_name_it_was_minted_with(tmp_path, monkeypatch):
    """A scheduled task's conversation is pre-titled, so no title is derived and none is written.

    On a channel that lists conversations, which is the one where a firing mints its own; without a
    list it runs in the viewed conversation and titles it like any other first turn.
    """
    calls: list = []

    async def title(model, first_message):
        calls.append(first_message)
        return "Something else"

    monkeypatch.setattr("kokua.core.titles.summarize_title", title)
    assistant = await Assistant.create(
        _config(tmp_path), _ConvCapturingChannel(), client_factory=lambda cid: MockAsyncModelClient(["done"])
    )

    await assistant._proactive("run the backup", task_name="nightly backup", task_id="nightly backup")
    await settle_titles(assistant)

    titles_stored = [item["title"] for item in assistant.list_conversations()]
    assert "nightly backup" in titles_stored
    assert calls == []


async def test_the_title_is_written_on_the_model_the_conversation_runs_on(tmp_path, monkeypatch):
    seen: list = []

    async def title(model, first_message):
        seen.append(model)
        return None

    monkeypatch.setattr("kokua.core.titles.summarize_title", title)
    config = _config(tmp_path, model="provider:conversation-model@http://host:1234")
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient(["ok"]))

    await assistant._handle(ChannelMessage(text="plan my trip", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)

    assert seen == ["provider:conversation-model@http://host:1234"]


async def test_an_error_writing_a_title_does_not_escape_its_task(tmp_path, monkeypatch):
    """Nobody awaits the title task, so an error in it would become an unretrieved exception on the
    event loop rather than anything a user or a caller sees (the rule invariant 6 in ``core/turns.py``
    states for unattended turns). Its own failures are already handled inside
    ``titles.summarize_title``; this covers the write and the push that follow it."""
    monkeypatch.setattr("kokua.core.titles.summarize_title", _fixed_title("Kauai trip planning"))
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["ok"]))

    async def boom(*args, **kwargs):
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(assistant._book, "retitle", boom)

    await assistant._handle(ChannelMessage(text="plan my trip", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)

    assert assistant._store.get(assistant._active_id).metadata["title"] == "plan my trip"


async def test_titles_are_not_generated_when_the_setting_is_off(tmp_path, monkeypatch):
    calls: list = []

    async def title(model, first_message):
        calls.append(first_message)
        return "Kauai trip planning"

    monkeypatch.setattr("kokua.core.titles.summarize_title", title)
    assistant = await Assistant.create(
        _config(tmp_path, generate_titles=False), FakeChannel(), client=MockAsyncModelClient(["ok"])
    )

    await assistant._handle(ChannelMessage(text="plan my trip", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)

    assert calls == []
    assert assistant._store.get(assistant._active_id).metadata["title"] == "plan my trip"


async def test_titles_can_be_turned_off_at_runtime(tmp_path, monkeypatch):
    """A hot setting: no restart, and the change is in config.toml under its own section afterwards."""
    calls: list = []

    async def title(model, first_message):
        calls.append(first_message)
        return "Kauai trip planning"

    monkeypatch.setattr("kokua.core.titles.summarize_title", title)
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient(["ok"])
    )

    await assistant._settings.apply_and_persist({"generate_titles": False})
    await assistant._handle(ChannelMessage(text="plan my trip", channel="fake"), conversation_id=assistant._active_id)
    await settle_titles(assistant)

    assert calls == []
    assert "generate_titles = false" in assistant._config.config_path.read_text(encoding="utf-8")


# A parent transcript with two complete turns. Turn one runs a tool, so its copy has to carry the
# tool result as well as the answer: the pairing is what a provider validates on the next request.
BRANCH_MESSAGES = [
    {"role": "system", "content": "you are kokua"},
    {"role": "user", "content": "first question"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "function": {"name": "clock", "arguments": "{}"}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "12:00"},
    {"role": "assistant", "content": "first answer"},
    {"role": "user", "content": "second question"},
    {"role": "assistant", "content": "second answer"},
]


async def _assistant_with_branchable_parent(tmp_path, **metadata):
    """An assistant whose active conversation holds BRANCH_MESSAGES, plus that session."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    parent = assistant._session
    parent.messages = [dict(message) for message in BRANCH_MESSAGES]
    parent.metadata["title"] = "Kauai trip"
    parent.metadata.update(metadata)
    assistant._store.save(parent)
    return assistant, parent


async def test_branch_copies_through_the_end_of_the_named_turn(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    branch_id = assistant._book.branch(parent.key, 1)

    branched = assistant._store.get(branch_id)
    assert [m.get("content") for m in branched.messages] == [
        "you are kokua",
        "first question",
        "",
        "12:00",
        "first answer",
    ]


async def test_branch_switches_the_view_and_leaves_the_parent_alone(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    branch_id = assistant._book.branch(parent.key, 1)

    assert assistant._active_id == branch_id
    assert assistant._store.get(parent.key).messages == BRANCH_MESSAGES


async def test_branch_of_the_last_turn_copies_the_whole_transcript(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    branch_id = assistant._book.branch(parent.key, 5)

    assert assistant._store.get(branch_id).messages == BRANCH_MESSAGES


async def test_branch_does_not_end_a_turn_at_a_loop_injected_message(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)
    parent.messages.insert(4, {"role": "user", "content": "continue", PROVENANCE_KEY: PROVENANCE_CONTINUATION})
    assistant._store.save(parent)

    branch_id = assistant._book.branch(parent.key, 1)

    # The injected nudge and the answer after it both belong to turn one.
    assert [m.get("content") for m in assistant._store.get(branch_id).messages][-2:] == ["continue", "first answer"]


async def test_branch_titles_itself_after_its_parent(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    branch_id = assistant._book.branch(parent.key, 1)

    assert assistant._store.get(branch_id).metadata["title"] == "Branch of Kauai trip"


async def test_branch_of_an_untitled_conversation_names_it_as_untitled(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)
    del parent.metadata["title"]
    assistant._store.save(parent)

    branch_id = assistant._book.branch(parent.key, 1)

    assert assistant._store.get(branch_id).metadata["title"] == "Branch of New conversation"


async def test_branch_keeps_only_the_metadata_of_the_turns_it_copied(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(
        tmp_path,
        usage={"1": {"calls": 1}, "5": {"calls": 2}},
        model={"1": "ollama:gemma", "5": "ollama:gemma"},
        subagent={"5": [{"task": "check the ferry times"}]},
    )

    branch_id = assistant._book.branch(parent.key, 1)

    branched = assistant._store.get(branch_id)
    assert branched.metadata["usage"] == {"1": {"calls": 1}}
    assert branched.metadata["model"] == {"1": "ollama:gemma"}
    assert "subagent" not in branched.metadata


async def test_branch_records_where_it_came_from(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    branch_id = assistant._book.branch(parent.key, 1)

    assert assistant._store.get(branch_id).metadata["branched_from"] == {
        "conversation_id": parent.key,
        "message_index": 1,
    }


async def test_branch_does_not_inherit_the_task_that_minted_its_parent(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path, task_id="morning-brief")

    branch_id = assistant._book.branch(parent.key, 1)

    assert "task_id" not in assistant._store.get(branch_id).metadata


async def test_branch_refuses_an_index_that_is_not_a_user_turn(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    with pytest.raises(TurnNotFound):
        assistant._book.branch(parent.key, 2)  # an assistant message
    with pytest.raises(TurnNotFound):
        assistant._book.branch(parent.key, 99)  # past the end
    with pytest.raises(TurnNotFound):
        assistant._book.branch(parent.key, -1)


async def test_branchable_answers_what_branch_would_accept(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    assert assistant._book.branchable(parent.key, 1)
    assert not assistant._book.branchable(parent.key, 2)


async def test_branch_conversation_switches_and_abandons_a_pending_question(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    branch_id = await assistant.branch_conversation(parent.key, 1)

    assert assistant._active_id == branch_id
    assert assistant._store.get(branch_id).metadata["title"] == "Branch of Kauai trip"


async def test_branch_conversation_reverts_active_id_on_build_failure(tmp_path):
    """branch reuses _activate verbatim, the same as select/new/delete, so a build failure on the
    new branch's agent must revert the active pointer exactly like it does for the other three."""
    from kokua.core.assistant import ModelClientError

    calls = {"n": 0}

    def factory(conversation_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the parent conversation builds fine; the branch's own build fails
            raise ModelClientError("model no longer available")
        return MockAsyncModelClient([])

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    parent = assistant._session
    parent.messages = [dict(message) for message in BRANCH_MESSAGES]
    parent.metadata["title"] = "Kauai trip"
    assistant._store.save(parent)
    original_id = assistant._active_id

    with pytest.raises(ModelClientError):
        await assistant.branch_conversation(parent.key, 1)

    # Reverts to the parent, which was active before the call; the branch's own session record
    # still lingers in the store, unused but harmless, the same best-effort revert new_conversation
    # documents.
    assert assistant._active_id == original_id


async def test_duplicate_copies_the_whole_transcript(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    copy_id = assistant._book.duplicate(parent.key)

    assert assistant._store.get(copy_id).messages == BRANCH_MESSAGES


async def test_duplicate_leaves_the_view_and_the_parent_alone(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    copy_id = assistant._book.duplicate(parent.key)

    assert copy_id != parent.key
    assert assistant._active_id == parent.key
    assert assistant._store.get(parent.key).messages == BRANCH_MESSAGES


async def test_duplicate_titles_itself_after_its_parent(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    copy_id = assistant._book.duplicate(parent.key)

    assert assistant._store.get(copy_id).metadata["title"] == "Copy of Kauai trip"


async def test_duplicate_of_an_untitled_conversation_names_it_as_untitled(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)
    del parent.metadata["title"]
    assistant._store.save(parent)

    copy_id = assistant._book.duplicate(parent.key)

    assert assistant._store.get(copy_id).metadata["title"] == "Copy of New conversation"


async def test_duplicate_keeps_the_metadata_of_every_turn(tmp_path):
    """Nothing is cut, so unlike a branch there is no per-turn map to filter."""
    assistant, parent = await _assistant_with_branchable_parent(
        tmp_path,
        usage={"1": {"calls": 1}, "5": {"calls": 2}},
        subagent={"5": [{"task": "check the ferry times"}]},
    )

    copy_id = assistant._book.duplicate(parent.key)

    copied = assistant._store.get(copy_id)
    assert copied.metadata["usage"] == {"1": {"calls": 1}, "5": {"calls": 2}}
    assert copied.metadata["subagent"] == {"5": [{"task": "check the ferry times"}]}


async def test_duplicate_records_where_it_came_from(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    copy_id = assistant._book.duplicate(parent.key)

    assert assistant._store.get(copy_id).metadata["copied_from"] == {"conversation_id": parent.key}


async def test_duplicate_does_not_inherit_the_task_that_minted_its_parent(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path, task_id="morning-brief")

    copy_id = assistant._book.duplicate(parent.key)

    assert "task_id" not in assistant._store.get(copy_id).metadata


async def test_duplicate_refuses_a_conversation_the_store_does_not_have(tmp_path):
    assistant, _ = await _assistant_with_branchable_parent(tmp_path)

    with pytest.raises(ConversationNotFound):
        assistant._book.duplicate("nosuchconversation")


async def test_duplicate_conversation_lists_the_copy_without_switching_to_it(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    copy_id = await assistant.duplicate_conversation(parent.key)

    assert assistant.active_id == parent.key
    assert {item["id"] for item in assistant.list_conversations()} == {parent.key, copy_id}


async def test_truncate_removes_the_named_turn_and_everything_after_it(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    removed = await assistant._book.truncate(parent.key, 5)

    assert removed == 2
    # Turn one survives whole, its tool call and the result answering it included: that pairing is
    # what a provider validates on the conversation's next request.
    assert [m.get("content") for m in assistant._store.get(parent.key).messages] == [
        "you are kokua",
        "first question",
        "",
        "12:00",
        "first answer",
    ]


async def test_truncate_leaves_the_conversation_where_it_was(tmp_path):
    """Same id, same active pointer: unlike branch and delete, the view does not move."""
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    await assistant._book.truncate(parent.key, 5)

    assert assistant._active_id == parent.key
    assert parent.key in assistant._store.list_keys()
    kept = assistant._store.get(parent.key)
    assert kept.metadata["title"] == "Kauai trip"
    # Not bumped, unlike `persist` and like `retitle`: the sidebar orders by when a conversation last
    # had activity in it, and tidying a record is not activity in it.
    assert kept.metadata["updated_at"] == parent.metadata["updated_at"]


async def test_truncating_the_first_turn_empties_the_conversation_and_drops_its_title(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    removed = await assistant._book.truncate(parent.key, 1)

    kept = assistant._store.get(parent.key)
    assert removed == 6
    # The system message is not a turn and stays behind: AIMU seeds it as part of a conversation's
    # first turn, so "emptied" means no user message is left, not that the list is empty.
    assert [m.get("role") for m in kept.messages] == ["system"]
    # Untitled again, so the sidebar reads it as a fresh conversation and the next turn derives a
    # title from what is actually in it.
    assert "title" not in kept.metadata


async def test_truncate_keeps_only_the_metadata_of_the_turns_it_kept(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(
        tmp_path,
        usage={"1": {"calls": 1}, "5": {"calls": 2}},
        model={"1": "ollama:gemma", "5": "ollama:gemma"},
        subagent={"5": [{"task": "check the ferry times"}]},
    )

    await assistant._book.truncate(parent.key, 5)

    kept = assistant._store.get(parent.key).metadata
    # Filtered, not remapped: a prefix cut leaves every surviving index where it was.
    assert kept["usage"] == {"1": {"calls": 1}}
    assert kept["model"] == {"1": "ollama:gemma"}
    assert "subagent" not in kept


async def test_truncate_rebuilds_the_agent_from_the_shortened_transcript(tmp_path):
    """The store is the single answer to what a conversation holds, so the built agent has to go."""
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)
    before = assistant._book.agent

    await assistant._book.truncate(parent.key, 5)

    after = assistant._book.agent
    assert after is not before
    # `agent.restore` (the path `build()` rebuilds through) strips every stored `system` message,
    # since the client tracks its own system prompt separately and re-prepends it per call; the
    # rebuilt agent's message list never carries one, whatever the store holds.
    assert [m.get("content") for m in after.model_client.messages] == [
        "first question",
        "",
        "12:00",
        "first answer",
    ]


async def test_truncate_keeps_the_conversations_turn_lock(tmp_path):
    """`discard` would take the lock with the agent, and this holds that lock while it writes."""
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)
    lock = assistant._registry.lock(parent.key)

    await assistant._book.truncate(parent.key, 5)

    assert assistant._registry.lock(parent.key) is lock


async def test_truncate_refuses_an_index_that_is_not_a_user_turn(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)
    # A loop-injected nudge carries the `user` role but sits *inside* a turn, between tool-calling
    # iterations, so cutting at it would end that turn before the answer it went on to produce.
    parent.messages.insert(4, {"role": "user", "content": "continue", PROVENANCE_KEY: PROVENANCE_CONTINUATION})
    assistant._store.save(parent)
    expected = list(parent.messages)

    for index in (2, 4, 99, -1):  # an assistant message, an injected nudge, past the end, below the start
        with pytest.raises(TurnNotFound):
            await assistant._book.truncate(parent.key, index)

    assert assistant._store.get(parent.key).messages == expected  # nothing partially applied


async def test_truncate_refuses_a_conversation_the_store_does_not_have(tmp_path):
    """A distinct error from `TurnNotFound`, and it takes the `exists` check to get one.

    `store.get` answers a missing key with a fresh empty `Session`, which has no user turn at any
    index, so without that check a deleted conversation would be reported as an unsaved turn.
    """
    assistant, _ = await _assistant_with_branchable_parent(tmp_path)

    with pytest.raises(ConversationNotFound):
        await assistant._book.truncate("deadbeefdeadbeef", 1)


async def test_truncate_re_asks_about_a_running_turn_once_it_holds_the_slot(tmp_path):
    """The caller's refusal check is made before an await, so the book asks again inside its hold.

    Without the re-check, a turn already in flight when that check ran could still slip through the
    window between the check and the gate. The predicate is injected because the tracker that can answer
    belongs to the assistant, and this layer must not import upward to reach it.
    """
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    with pytest.raises(TurnInFlight):
        await assistant._book.truncate(parent.key, 5, turn_running=lambda _: True)

    assert assistant._store.get(parent.key).messages == BRANCH_MESSAGES  # nothing was cut


async def test_a_turn_after_a_truncation_continues_from_the_shortened_transcript(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient(["third answer"]))
    conversation = assistant._session
    conversation.messages = [dict(message) for message in BRANCH_MESSAGES]
    conversation.metadata["title"] = "Kauai trip"
    assistant._store.save(conversation)

    await assistant._book.truncate(conversation.key, 5)
    await assistant._handle(ChannelMessage(text="third question", channel="fake"), conversation_id=conversation.key)

    contents = [m.get("content") for m in assistant._store.get(conversation.key).messages]
    assert "second question" not in contents  # the deleted turn is not in the model's context either
    assert "third question" in contents
    assert "third answer" in contents


async def test_truncate_conversation_reports_what_it_removed(tmp_path):
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)

    assert await assistant.truncate_conversation(parent.key, 5) == 2


async def test_truncate_conversation_refuses_while_that_conversation_has_a_turn_running(tmp_path):
    """Refused outright rather than queued behind the turn.

    Mostly a bounded wait, since the web front end applies controls on the one task reading its socket,
    and it keeps a deletion from applying minutes later and silently taking a turn that arrived in the
    meantime with it. Partly correctness too: a turn queued behind the cut would run on the agent the
    truncation dropped and have its answer discarded by `persist`.

    Its counterpart is `test_truncate_conversation_waits_for_a_non_turn_gate_holder_rather_than_refusing`,
    which pins the *mechanism* this one only exercises: the refusal asks the turn tracker rather than the
    gate slot. Neither is redundant with the other, and deleting that one would leave nothing saying a
    title write or a delete must be waited out instead of reported as a running turn.
    """
    import time

    from aimu.aio import RunHandle

    from kokua.core.turn_registry import TurnInfo

    client = BlockingModelClient()
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=client)
    conversation_id = assistant._active_id

    # Tracked the way `Assistant._serve_channel` tracks a reactive turn: it wraps `_handle` in a
    # RunHandle and registers that at submit time. Calling `_handle` directly is what a test has, and
    # the tracker is what `turn_running` reads, so the registration has to be made here too.
    handle = RunHandle.start(
        assistant._handle(ChannelMessage(text="a long job", channel="fake"), conversation_id=conversation_id)
    )
    assistant._tracker.add(conversation_id, TurnInfo(handle=handle, started=time.monotonic(), preview="a long job"))
    await asyncio.wait_for(client.started.wait(), timeout=5)

    try:
        with pytest.raises(TurnInFlight):
            await asyncio.wait_for(assistant.truncate_conversation(conversation_id, 1), timeout=5)
    finally:
        client.release.set()
        await handle.task

    # The turn ran to completion and kept its transcript: the refusal cut nothing.
    assert any(m.get("content") == "a long job" for m in assistant._store.get(conversation_id).messages)


async def test_truncate_conversation_allows_a_turn_running_elsewhere(tmp_path):
    """The refusal is per conversation, like the turn slot it stands in for."""
    import time

    from aimu.aio import RunHandle

    from kokua.core.turn_registry import TurnInfo

    clients: dict = {}

    def factory(conversation_id):
        client = BlockingModelClient() if not clients else MockAsyncModelClient(["ok"])
        clients[conversation_id] = client
        return client

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client_factory=factory)
    busy_id = assistant._active_id
    blocking = clients[busy_id]
    await assistant.new_conversation()
    tidy_id = assistant._active_id
    tidy = assistant._store.get(tidy_id)
    tidy.messages = [dict(message) for message in BRANCH_MESSAGES]
    assistant._store.save(tidy)

    handle = RunHandle.start(
        assistant._handle(ChannelMessage(text="a long job", channel="fake"), conversation_id=busy_id)
    )
    assistant._tracker.add(busy_id, TurnInfo(handle=handle, started=time.monotonic(), preview="a long job"))
    await asyncio.wait_for(blocking.started.wait(), timeout=5)

    try:
        assert assistant.turn_running(busy_id) is True
        assert await asyncio.wait_for(assistant.truncate_conversation(tidy_id, 5), timeout=5) == 2
    finally:
        blocking.release.set()
        await handle.task


async def test_truncate_conversation_waits_for_a_non_turn_gate_holder_rather_than_refusing(tmp_path):
    """The refusal asks the turn tracker, not "is this conversation's gate slot held".

    Every `gate.turn` holder takes that slot, including a title write and a delete, so refusing on the
    slot would report a turn in flight when none is running and tell the user to stop something that
    does not exist. A non-turn holder is short, so waiting it out is correct.
    """
    assistant, parent = await _assistant_with_branchable_parent(tmp_path)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold_the_slot():
        async with assistant._gate.turn(parent.key):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(hold_the_slot())
    await asyncio.wait_for(holding.wait(), timeout=5)
    truncating = asyncio.create_task(assistant.truncate_conversation(parent.key, 5))
    try:
        await asyncio.sleep(0.05)  # long enough for it to reach the gate and block there
        assert not truncating.done()  # waiting on the slot, not refused outright
    finally:
        release.set()
    assert await asyncio.wait_for(truncating, timeout=5) == 2
    await holder
