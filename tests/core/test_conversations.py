"""ConversationBook: the store, the active pointer, and switching between conversations."""

from __future__ import annotations


import pytest

from aimu.aio.channels.base import ChannelMessage

from kokua.core.assistant import Assistant
from tests.channels import FakeChannel, _ConvCapturingChannel, _config
from tests.helpers import MockAsyncModelClient


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
