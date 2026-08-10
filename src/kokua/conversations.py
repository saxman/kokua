"""Conversations: the session store, the per-conversation agent cache, and the active pointer.

``ConversationBook`` owns the three things that must move together when the user switches
conversations: the persisted ``Session`` records, the ``AgentRegistry`` entry backing each one, and
which conversation is currently being viewed. Keeping them in one object is what makes the
set-pointer/build/revert rule below expressible once instead of three times.

It has no channel dependency. The active-pointer change is published through an injected
``on_active_change`` callback (the assistant passes ``ChannelUI.set_active_conversation``), so this
module stays about state and the transport concern stays in ``channels/``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Callable, Optional

from aimu import aio
from aimu.sessions import Session, TinyDBSessionStore

from .agent_registry import AgentRegistry
from .config import AssistantConfig
from .messages import compact_message_images, derive_title
from .turn_gate import TurnGate


def _now() -> str:
    return datetime.now().isoformat()


class ConversationBook:
    def __init__(
        self,
        store: TinyDBSessionStore,
        gate: TurnGate,
        config: AssistantConfig,
        *,
        on_active_change: Callable[[str], None],
    ):
        self._store = store
        self._gate = gate
        self._config = config
        self._on_active_change = on_active_change
        self._registry: Optional[AgentRegistry] = None
        self._active_id: str = ""

    def bind_registry(self, registry: AgentRegistry) -> None:
        """Attach the agent cache, once it exists.

        Two-phase because the registry's agent builder needs callbacks bound to the assistant, which
        in turn needs this book: the store and the active pointer are readable before any agent can
        be built (``adopt_most_recent`` deliberately does not build one), so the cycle breaks here.
        """
        self._registry = registry

    # --- the active conversation ------------------------------------------------------------

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def agent(self) -> aio.SkillAgent:
        """The active conversation's agent (built on demand by the registry)."""
        return self._registry.get(self._active_id)

    @property
    def session(self) -> Session:
        """The active conversation's persisted session (fetched fresh each access)."""
        return self._store.get(self._active_id)

    def adopt_most_recent(self) -> str:
        """Point at the most-recently-updated conversation, minting one if the store is empty.

        Used at startup, before any agent exists to build; the caller builds it afterwards.
        """
        self._active_id = self.most_recent_or_new().key
        self._on_active_change(self._active_id)
        return self._active_id

    def most_recent_or_new(self) -> Session:
        """The most-recently-updated session, creating a fresh empty one if the store is empty."""
        keys = self._store.list_keys()
        if keys:
            sessions = [self._store.get(key) for key in keys]
            sessions.sort(key=lambda session: session.metadata.get("updated_at", ""), reverse=True)
            return sessions[0]
        return self.new_session()

    def new_session(self, title: Optional[str] = None) -> Session:
        """Mint and persist an empty session, optionally pre-titled."""
        now = _now()
        metadata = {"created_at": now, "updated_at": now}
        if title:
            metadata["title"] = title
        session = Session(key=uuid.uuid4().hex, metadata=metadata)
        self._store.save(session)
        return session

    def _activate(self, conversation_id: str, *, revert_to: str) -> None:
        """Point the active pointer at ``conversation_id`` and build its agent eagerly.

        If the build fails the pointer reverts to ``revert_to`` before re-raising, so a caller is
        never left active on a conversation whose agent doesn't work. Building eagerly is the point:
        it forces the failure to surface here, where it can still be undone, rather than at the
        first access during a turn.
        """
        self._active_id = conversation_id
        self._on_active_change(self._active_id)
        try:
            self._registry.get(self._active_id)
        except Exception:
            self._active_id = revert_to
            self._on_active_change(self._active_id)
            raise

    # --- CRUD ---------------------------------------------------------------------------------

    def list(self) -> list[dict]:
        """All conversations as {id, title, updated_at, active}, most-recently-updated first."""
        items = [
            {
                "id": key,
                "title": session.metadata.get("title") or "New conversation",
                "updated_at": session.metadata.get("updated_at", ""),
                "active": key == self._active_id,
            }
            for key, session in ((key, self._store.get(key)) for key in self._store.list_keys())
        ]
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return items

    def create(self) -> str:
        """Start and switch to a new, empty conversation; returns its id.

        The previous conversation's turn (if any) keeps running in the background -- switching does
        not cancel it. On a build failure the active pointer reverts to the previous conversation
        (see ``_activate``); the new session record itself lingers in the store, unused but harmless
        (it mirrors an ordinary empty conversation the user never sent to).
        """
        previous_id = self._active_id
        session = self.new_session()
        self._activate(session.key, revert_to=previous_id)
        return session.key

    def select(self, conversation_id: str) -> None:
        """Switch the active conversation to an existing one; its agent (re)builds from the store.

        The previous conversation's turn (if any) keeps running in the background -- switching does
        not cancel it. On a build failure the active pointer reverts (see ``_activate``).
        """
        self._activate(conversation_id, revert_to=self._active_id)

    async def delete(self, conversation_id: str, *, cancel_turn: Callable[[str], None]) -> bool:
        """Delete a conversation, switching away from it if it was the active one.

        Returns whether the deleted conversation was the active one (so the caller knows a switch
        happened). ``cancel_turn`` cancels that conversation's in-flight turn: unlike select/create
        this DOES cancel, but only the deleted conversation's own, because there is no conversation
        left for it to keep persisting to.

        On a failure to build the replacement's agent, ``_activate`` reverts the pointer to the
        just-deleted id. Its store record and registry entry are already gone by then (the delete is
        not rolled back), so this is a best-effort revert: it keeps the pointer off some OTHER
        untested conversation, but a caller that touches ``agent`` afterward hits the same build
        failure again. The front end is expected to surface the re-raised error and stop, not retry.
        """
        cancel_turn(conversation_id)
        deleting_active = conversation_id == self._active_id
        previous_id = self._active_id
        async with self._gate.exclusive():
            self._store.delete(conversation_id)
            self._registry.discard(conversation_id)
            if deleting_active:
                self._active_id = self.most_recent_or_new().key
                self._on_active_change(self._active_id)
        if deleting_active:
            self._activate(self._active_id, revert_to=previous_id)
        return deleting_active

    # --- persistence --------------------------------------------------------------------------

    def persist(self, conversation_id: str) -> bool:
        """Snapshot ``conversation_id``'s agent messages onto its session and save. Returns True if a
        title was just derived (first user message), so a caller can refresh the conversation list."""
        session = self._store.get(conversation_id)
        agent = self._registry.get(conversation_id)
        session.messages = compact_message_images(
            [dict(message) for message in agent.model_client.messages], self._config.images_path
        )
        title_set = False
        if not session.metadata.get("title"):
            title = derive_title(session.messages)
            if title:
                session.metadata["title"] = title
                title_set = True
        session.metadata["updated_at"] = _now()
        self._store.save(session)
        return title_set

    def record_plan_metadata(self, result, conversation_id: str) -> None:
        """Record a planned turn's reviewer verdicts and verbose trace under the turn's user-message
        index, so reload replays them. No-op when the turn did not commit (e.g. plan rejected)."""
        if not result.committed or result.user_index < 0:
            return
        session = self._store.get(conversation_id)
        changed = False
        if result.subagent_events:
            session.metadata.setdefault("subagent", {})[str(result.user_index)] = result.subagent_events
            changed = True
        if result.trace:
            session.metadata.setdefault("trace", {})[str(result.user_index)] = result.trace
            changed = True
        if changed:
            self._store.save(session)

    def exists(self, conversation_id: str) -> bool:
        """Whether a conversation is still in the store.

        Checked against ``list_keys()`` rather than ``store.get()``, because the store returns an
        empty ``Session`` for a missing key, which would resurrect a deleted conversation as a blank
        one rather than reporting it gone.
        """
        return conversation_id in self._store.list_keys()

    def get(self, conversation_id: str) -> Session:
        return self._store.get(conversation_id)

    def save(self, session: Session) -> None:
        self._store.save(session)

    def touch(self, session: Session) -> None:
        session.metadata["updated_at"] = _now()
        self._store.save(session)
