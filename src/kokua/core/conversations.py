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
from typing import Callable, Optional, Union

from aimu import aio
from aimu.sessions import Session, TinyDBSessionStore

from kokua.core.agent_registry import AgentRegistry
from kokua.config import AssistantConfig
from kokua.core.messages import compact_message_images, derive_title
from kokua.core.turn_gate import TurnGate


# Shortest leading fragment of a conversation id that ``resolve`` will accept. Long enough that a
# prefix hit is not a coincidence among 32-hex ids.
ID_PREFIX_MIN = 6


def _now() -> str:
    return datetime.now().isoformat()


def _merge_subagent_events(session: Session, user_index: int, events: list[dict]) -> None:
    """Append a turn's sub-agent cards under its user-message index.

    Extends rather than assigns: one turn can produce both reviewer verdict cards (a workflow turn)
    and spawn cards, recorded by different callers.
    """
    session.metadata.setdefault("subagent", {}).setdefault(str(user_index), []).extend(events)


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
        sessions = self.sessions()
        return sessions[0] if sessions else self.new_session()

    def new_session(self, title: Optional[str] = None, task_id: Optional[str] = None) -> Session:
        """Mint and persist an empty session, optionally pre-titled and attributed to a task.

        ``task_id`` marks a conversation a scheduled task minted, which is how the sidebar nests it
        under that task. It is the task's id rather than its name because a name is optional and
        ``update_scheduled_task`` can change it, so only the id survives an edit.
        """
        now = _now()
        metadata = {"created_at": now, "updated_at": now}
        if title:
            metadata["title"] = title
        if task_id:
            metadata["task_id"] = task_id
        session = Session(key=uuid.uuid4().hex, metadata=metadata)
        self._store.save(session)
        return session

    # --- agents -------------------------------------------------------------------------------

    def agent_for(self, conversation_id: str) -> aio.SkillAgent:
        """Any conversation's agent, built on demand. No active-pointer swap: the registry looks up
        by id, which is what lets a proactive run work on a conversation nobody is viewing."""
        return self._registry.get(conversation_id)

    def pin(self, conversation_id: str) -> None:
        """Hold a conversation's agent in the cache for the duration of a turn.

        The registry evicts LRU. Without this, another conversation's turn can evict this one's agent
        mid-run, and persisting afterwards would rebuild a stale agent from the store and silently
        lose the turn's output.
        """
        self._registry.pin(conversation_id)

    def unpin(self, conversation_id: str) -> None:
        self._registry.unpin(conversation_id)

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

    def sessions(self) -> list[Session]:
        """Every stored conversation, most-recently-updated first.

        The one place the store's key-then-get walk and the ``updated_at`` ordering live: ``list()``
        projects it for the sidebar, ``most_recent_or_new`` takes its head, and the agent's read-only
        conversation tools scan it. Re-read on every call, which is the point -- a conversation a
        background turn just persisted has to show up.

        Costs one store read per conversation (TinyDB re-parses the file each time), which is already
        what a sidebar push costs. If that ever matters, a bulk read belongs in AIMU's store, and this is
        the single seam it would land behind.
        """
        sessions = [self._store.get(key) for key in self._store.list_keys()]
        sessions.sort(key=lambda session: session.metadata.get("updated_at", ""), reverse=True)
        return sessions

    def sessions_for_task(self, task_id: str) -> list[Session]:
        """The conversations a scheduled task minted, oldest first.

        Ordered by ``created_at`` rather than the ``updated_at`` :meth:`sessions` uses: retention
        prunes a task's runs in the order they were minted, and a late turn or edit touching an older
        run must not make it look like the newest one. ``updated_at`` then the key break a tie, so a
        session stored before conversations recorded ``created_at`` still sorts deterministically.
        """
        owned = [s for s in self.sessions() if s.metadata.get("task_id") == task_id]
        owned.sort(key=lambda s: (s.metadata.get("created_at", ""), s.metadata.get("updated_at", ""), s.key))
        return owned

    def retag_task(self, old_task_id: str, new_task_id: str) -> int:
        """Re-point every conversation a task minted at the task's new name. Returns how many moved.

        A task's name is its identity, so a rename would otherwise orphan its history: the sidebar
        stops nesting the runs under their task, and retention stops counting them against its cap.
        """
        sessions = self.sessions_for_task(old_task_id)
        for session in sessions:
            session.metadata["task_id"] = new_task_id
            self._store.save(session)
        return len(sessions)

    def list(self) -> list[dict]:
        """All conversations as {id, title, updated_at, active, task_id}, most-recently-updated first.

        ``task_id`` is the scheduled task that minted the conversation, or None for one the user
        started. Nothing is filtered out here: a front end that groups task conversations under their
        task does that grouping itself, so the agent's conversation tools still see every conversation.
        """
        return [
            {
                "id": session.key,
                "title": session.metadata.get("title") or "New conversation",
                "updated_at": session.metadata.get("updated_at", ""),
                "active": session.key == self._active_id,
                "task_id": session.metadata.get("task_id"),
            }
            for session in self.sessions()
        ]

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

    def record_workflow_metadata(self, result, conversation_id: str) -> None:
        """Record a workflow turn's sub-agent cards and verbose trace under the turn's user-message
        index, so reload replays them. No-op when the turn did not commit (e.g. a rejected plan)."""
        if not result.committed or result.user_index < 0:
            return
        session = self._store.get(conversation_id)
        changed = False
        if result.subagent_events:
            _merge_subagent_events(session, result.user_index, result.subagent_events)
            changed = True
        if result.trace:
            session.metadata.setdefault("trace", {})[str(result.user_index)] = result.trace
            changed = True
        if changed:
            self._store.save(session)

    def record_turn_provenance(
        self,
        events: list[dict],
        model: str,
        user_index: int,
        conversation_id: str,
        thinking: Optional[Union[bool, str]] = None,
        failure: Optional[str] = None,
    ) -> None:
        """Persist what produced a turn's output: its sub-agent activity, the model that answered, the
        reasoning effort it ran at, and why it stopped early if it did.

        The cards are what reload replays. The model and the effort are recorded per turn rather than
        once per conversation because a conversation outlives the config that started it:
        ``[assistant].model``, ``[assistant].thinking``, and an agent's own declarations are all read at
        startup, so two turns of one conversation can have been answered by different models at
        different efforts. Each spawn card carries its own worker's pair, since a worker need not match.

        ``thinking`` is guarded on ``is not None`` rather than truthiness, because ``False`` means
        "reasoning off" and has to be distinguishable from "nothing configured", which is the common
        case and stays out of the file.

        ``failure`` is the reason a turn ended in an error, and belongs in metadata rather than in
        ``session.messages`` for the reason every other entry here does: the messages are what this
        conversation's agent rebuilds its context from, so a synthesized assistant turn saying "this
        failed" would come back to the model as its own prior words. An unattended run needs it most --
        its status line goes to whichever conversation the user is viewing, leaving the run's own
        conversation with no account of why it has only half a turn in it.
        """
        if user_index < 0 or not (events or model or thinking is not None or failure):
            return
        session = self._store.get(conversation_id)
        if events:
            _merge_subagent_events(session, user_index, events)
        if model:
            session.metadata.setdefault("model", {})[str(user_index)] = model
        if thinking is not None:
            session.metadata.setdefault("thinking", {})[str(user_index)] = thinking
        if failure:
            session.metadata.setdefault("failure", {})[str(user_index)] = failure
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

    def resolve(self, conversation_id: str) -> Optional[Session]:
        """The stored session for an id, or ``None``.

        A unique leading fragment of at least ``ID_PREFIX_MIN`` characters also resolves; an ambiguous
        one does not, so a caller can report that rather than open the wrong conversation. The fragment
        exists because a caller that saw a 32-hex id in a listing is apt to shorten it, and it is long
        enough that a prefix hit is not a coincidence.

        Reads only the store, never ``agent_for``: resolving must stay cheap and side-effect-free, and
        building an agent is neither (see ``toolsets/conversations.py`` for the full reasoning).
        """
        wanted = (conversation_id or "").strip().strip("'\"`")
        if not wanted:
            return None
        if self.exists(wanted):
            return self.get(wanted)
        if len(wanted) < ID_PREFIX_MIN:
            return None
        matches = [session for session in self.sessions() if session.key.startswith(wanted)]
        return matches[0] if len(matches) == 1 else None

    def save(self, session: Session) -> None:
        self._store.save(session)

    def touch(self, session: Session) -> None:
        session.metadata["updated_at"] = _now()
        self._store.save(session)
