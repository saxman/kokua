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

import copy
import uuid
from datetime import datetime
from typing import Callable, Optional, Union

from aimu import aio
from aimu.sessions import Session, TinyDBSessionStore

from kokua.core.agent_registry import AgentRegistry
from kokua.config import AssistantConfig
from kokua.core.messages import TITLE_MAX, compact_message_images, derive_title, is_user_turn
from kokua.core.turn_gate import TurnGate


# Shortest leading fragment of a conversation id that ``resolve`` will accept. Long enough that a
# prefix hit is not a coincidence among 32-hex ids.
ID_PREFIX_MIN = 6

# What a conversation with no title yet is called wherever one is named to a user, so the sidebar and
# the /switch reply cannot drift apart on it.
UNTITLED = "New conversation"

# What a forked conversation is called, so a branch is never mistaken for its parent in a sidebar
# that would otherwise show the same title twice (both derive from the same first user message).
BRANCH_TITLE_PREFIX = "Branch of "

# What a whole-conversation copy is called, for the same reason a branch is prefixed: two rows deriving
# their title from the same first user message are otherwise indistinguishable in a sidebar.
COPY_TITLE_PREFIX = "Copy of "

# The session-metadata maps keyed by a turn's user-message index. A branch copies a *prefix* of the
# transcript, so indices do not move and these are filtered rather than remapped. That is only true
# while a branch keeps the parent's messages from index 0 onward; anything that changes what index 0
# is has to revisit this.
TURN_KEYED_METADATA = ("subagent", "trace", "model", "thinking", "failure", "usage")


class TurnNotFound(Exception):
    """An operation named a turn the stored transcript does not have.

    Raised by branching and by truncation rather than acting at some nearby point, because the caller
    asked for one exchange and silently giving it another is the failure a user cannot see. The case
    that happens is a turn whose ``persist`` has not landed yet; a page holding an index from before an
    earlier truncation is the other.
    """


class ConversationNotFound(Exception):
    """An operation named a conversation the store does not have.

    Kept distinct from :class:`TurnNotFound` because the two want different sentences: a conversation
    that is gone is nothing the user can act on, while a turn that is not stored yet becomes actionable
    a moment later. Telling them apart needs ``exists``, since ``store.get`` answers a missing key with
    a fresh empty ``Session``, which has no user turn at any index and would otherwise be reported as an
    unsaved turn.
    """


class TurnInFlight(Exception):
    """A destructive edit was asked for on a conversation whose turn is still running.

    Raised first by ``Assistant.truncate_conversation``, because the tracker that knows which
    conversations have a turn in flight belongs to the assistant, while the book holds only the gate,
    which can wait for a turn but cannot report that one exists. The book raises it too, from inside its
    hold, but only through the predicate that method injects, for the same reason. Declared here with
    the other refusals of the same operation, so a caller catching them finds them together and the
    front end words them in one place.
    """


def turn_end(messages: list[dict], user_index: int) -> Optional[int]:
    """The exclusive end of the turn opened at ``user_index``, or None if no user turn is there.

    The end is the next message the user actually sent, or the end of the transcript. Injected loop
    turns are skipped (see :func:`kokua.core.messages.is_user_turn`): they carry the ``user`` role but
    sit *inside* a turn, between tool-calling iterations, so ending there would cut a turn off before
    the answer it produced.

    A turn boundary is also the only cut a transcript survives. Anywhere else can fall between an
    assistant message holding ``tool_calls`` and the ``tool`` messages answering them, which a provider
    rejects on the next request rather than here, where it could still be reported.
    """
    if not 0 <= user_index < len(messages) or not is_user_turn(messages[user_index]):
        return None
    for index in range(user_index + 1, len(messages)):
        if is_user_turn(messages[index]):
            return index
    return len(messages)


def _metadata_before(metadata: dict, key: str, cut: int) -> dict:
    """The entries of a turn-keyed metadata map whose index survives a prefix cut at ``cut``.

    Shared by :meth:`ConversationBook.branch` and :meth:`ConversationBook.truncate`, which both keep
    ``messages[:cut]`` (a new conversation's whole transcript there, this conversation's remainder
    here) and so both need the same answer to "which turn-indexed records survive with it". A prefix
    cut leaves every surviving index exactly where it was, so a turn-keyed map is filtered rather than
    remapped; that stops being true the moment anything changes what index 0 of a stored transcript is.

    ``index.isdigit()`` guards against a key this map was never meant to hold: every real entry is
    written as ``str(user_index)`` by ``record_turn_provenance``/``record_workflow_metadata``, so a
    non-digit key would only appear from a hand-edited or foreign session file, and comparing it to
    ``cut`` would raise rather than simply being excluded.

    Callers differ only in what they do with the result: :meth:`branch` deep-copies it into a fresh
    session and sets it only when non-empty; :meth:`truncate` rebinds its own session's key to the
    filtered map and removes the key entirely when nothing survives. Both dispositions stay at the call site.
    """
    return {index: value for index, value in metadata.get(key, {}).items() if index.isdigit() and int(index) < cut}


def _now() -> str:
    return datetime.now().isoformat()


def _fragment(conversation_id: str) -> str:
    """The comparable form of a typed id. A user copying one out of a listing often brings quotes."""
    return (conversation_id or "").strip().strip("'\"`")


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
                "title": session.metadata.get("title") or UNTITLED,
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

    def branchable(self, conversation_id: str, user_index: int) -> bool:
        """Whether :meth:`branch` would accept this turn, asked without raising.

        The front end's question, so a branch control is only ever offered for a turn that can
        actually be branched. Reads the same ``turn_end`` the branch does, so the offer and the
        operation cannot disagree.
        """
        return turn_end(self._store.get(conversation_id).messages, user_index) is not None

    def branch(self, conversation_id: str, user_index: int) -> str:
        """Fork a conversation at one of its turns into a new one, switch to it, and return its id.

        The branch holds a copy of the parent's transcript through the end of the named turn, and the
        two are ordinary independent conversations from here: nothing about the parent changes, and
        deleting either leaves the other whole. A copy rather than a reference to the parent for that
        reason, and because the duplication is text (images are already content-addressed references,
        which a copy shares rather than duplicates).

        The mirror image of :meth:`truncate`, named from the other side of one boundary: this keeps
        ``messages[:turn_end(...)]`` in a *new* conversation, and truncate keeps ``messages[:user_index]``
        in *this* one. A reader who has followed either should know they are looking at the other.

        Read from the store, never from ``agent_for``, for the reason ``toolsets/conversations.py``
        gives: building an agent is neither cheap nor side-effect-free, and a running turn mutates the
        live message list in place while the store holds a snapshot written once per turn. A turn in
        flight on the parent is therefore invisible here, which is correct: the cut is always behind it.

        No turn-gate hold. The call reads a snapshot of the parent and writes a brand-new key, so there
        is nothing for another turn's ``persist`` (which saves whole sessions, under their own keys) to
        collide with.
        """
        parent = self._store.get(conversation_id)
        cut = turn_end(parent.messages, user_index)
        if cut is None:
            raise TurnNotFound(f"Conversation {conversation_id} has no user turn at message {user_index}.")
        previous_id = self._active_id
        title = f"{BRANCH_TITLE_PREFIX}{parent.metadata.get('title') or UNTITLED}"[:TITLE_MAX]
        # Titled at birth, which is also what keeps `persist` from deriving one from the inherited
        # first user message and handing the branch its parent's title.
        session = self.new_session(title=title)
        # A one-level copy of each message, not a deep one: a message dict's values (role, content,
        # provenance, tool_calls) are only ever replaced wholesale, never mutated in place, once a turn
        # commits it, so the branch and the parent sharing the same nested lists/dicts is safe under
        # that discipline. The metadata slices just below get `copy.deepcopy` instead because their
        # values (usage counters, a trace list) are the opposite: a subsystem reading `session.metadata`
        # off a live in-memory session could still append to one in place. Both copies are equally safe
        # today only because `TinyDBSessionStore.get` reparses from JSON on every call, handing back
        # fresh objects either way; the distinction matters the day a store hands back live references.
        session.messages = [dict(message) for message in parent.messages[:cut]]
        session.metadata["branched_from"] = {"conversation_id": conversation_id, "message_index": user_index}
        for key in TURN_KEYED_METADATA:
            kept = _metadata_before(parent.metadata, key, cut)
            if kept:
                session.metadata[key] = copy.deepcopy(kept)
        # `task_id` is deliberately not inherited: a branch of a scheduled run is the user's
        # conversation, not another run of that task, and inheriting it would expose the branch to
        # that task's retention pruning.
        self._store.save(session)
        self._activate(session.key, revert_to=previous_id)
        return session.key

    def duplicate(self, conversation_id: str) -> str:
        """Copy a whole conversation into a new one, without switching to it. Returns its id.

        A branch asks about a turn; this asks about a conversation, which is why it takes no index,
        keeps every turn's metadata rather than filtering it through :func:`_metadata_before`, and does
        not need ``turn_end``: nothing is cut, so there is no boundary to land on. The copy and the
        original are ordinary independent conversations from here, exactly as a branch and its parent
        are.

        Unlike :meth:`branch` it does not activate what it wrote. The control this serves is a sidebar
        row's, so it acts on a conversation the user is not necessarily reading, and moving the view
        onto the copy would take them out of the one they are. That also leaves this the one
        conversation-minting method with no ``_activate`` and so no pointer to revert: nothing here can
        fail on an agent build, because no agent is built.

        Read from the store, never from ``agent_for``, and unheld, for the reasons :meth:`branch` gives
        in full: the read is a snapshot and the write is a brand-new key. A turn in flight on the
        original is therefore invisible, so the copy is that conversation as last persisted, which is
        the same thing a branch of its last turn would give.
        """
        if not self.exists(conversation_id):
            raise ConversationNotFound(f"Conversation {conversation_id} is not in the store.")
        original = self._store.get(conversation_id)
        title = f"{COPY_TITLE_PREFIX}{original.metadata.get('title') or UNTITLED}"[:TITLE_MAX]
        # Titled at birth for the reason `branch` documents: it is what stops `persist` deriving a title
        # from the inherited first user message and handing the copy its original's title.
        session = self.new_session(title=title)
        # The same copy discipline as `branch`: one level per message, since a committed message's values
        # are replaced wholesale rather than mutated, and a deep copy of the metadata maps, whose values a
        # subsystem holding a live session could still append to in place.
        session.messages = [dict(message) for message in original.messages]
        session.metadata["copied_from"] = {"conversation_id": conversation_id}
        for key in TURN_KEYED_METADATA:
            if key in original.metadata:
                session.metadata[key] = copy.deepcopy(original.metadata[key])
        # `task_id` is deliberately not inherited, as in `branch`: a copy of a scheduled run is the
        # user's conversation, not another run of that task, and inheriting it would expose the copy to
        # that task's retention pruning.
        self._store.save(session)
        return session.key

    async def truncate(
        self, conversation_id: str, user_index: int, *, turn_running: Optional[Callable[[str], bool]] = None
    ) -> int:
        """Delete the turn opened at ``user_index`` and every turn after it. Returns messages removed.

        The mirror image of :meth:`branch`, named from the other side of one boundary: a branch keeps
        ``messages[:turn_end(...)]`` in a *new* conversation, and this keeps ``messages[:user_index]`` in
        *this* one. A reader who has followed either should know they are looking at the other. What is
        deleted is whole turns: the named turn's user message, its reasoning, its tool calls and the
        results answering them, its answer, and its recorded cards.

        A turn boundary is the only cut a transcript survives. Everything before a message the user
        actually sent is complete turns, so the remainder can never end between an assistant message
        holding ``tool_calls`` and the ``tool`` messages answering them, which a provider rejects on the
        conversation's next request rather than here, where it can still be reported. Validity is asked
        through ``turn_end`` rather than an inline index check, so "there is a turn here" means the same
        thing to a branch, to a truncation, and to the control a front end offers for either.

        The per-turn metadata maps are filtered through :func:`_metadata_before`, the same helper
        :meth:`branch` uses; a map left with nothing is removed rather than stored empty, so a tidied
        conversation reads like one that never had that kind of record. See that function for why the
        filter, not a remap, is what a prefix cut needs.

        The title is dropped when no user turn survives, which makes an emptied conversation
        indistinguishable from a fresh one: the sidebar shows it as untitled, and the next turn's
        ``persist`` derives a placeholder that ``Assistant._spawn_title`` replaces. Keeping the old title
        would leave the conversation named after a turn that is gone. Note the system message survives a
        cut at the first turn (AIMU seeds it as part of that turn), so "emptied" is about user turns
        rather than about the list being empty.

        ``updated_at`` is deliberately *not* bumped, unlike :meth:`persist` and like :meth:`retitle`.
        The timestamp orders the sidebar by when a conversation last had activity in it, and deleting
        turns is an edit of a record rather than activity in it; bumping it would shuffle every
        conversation the user tidies to the top of the list.

        Held under this conversation's own turn slot, like :meth:`delete` and :meth:`retitle`: the store
        saves whole sessions, so a read-modify-write racing a turn's ``persist`` would revert one of
        them. That hold is why the cut can never be interleaved with a turn's own save, since a turn
        holds the same slot across its ``persist``. Being one ``gate.turn`` makes this bound by
        invariant 1 in ``core/turns.py``, so a caller already holding a turn must not reach here. A
        conversation with a turn actually in flight is refused a layer up, in
        ``Assistant.truncate_conversation``; see that method for what the refusal covers that the hold
        does not.

        ``turn_running`` re-asks that refusal question *inside* the hold, so a turn already in flight when
        the caller's check ran cannot slip through the window between the check and the hold. It is
        injected rather than looked up here because the two halves live in different places: the book
        holds the gate, and the tracker that knows which conversations have a turn in flight belongs to
        the assistant, which this layer must not import upward to reach. Left as None (a caller with no
        tracker to ask, which every test of the book alone is) the hold is the only guard, exactly as
        before.

        The ``exists`` check is made *inside* that same hold, not before it, even though entering the
        hold is itself an ``await``. A check made before the hold answers a question about the moment
        before the wait, not the moment after it: a concurrent :meth:`delete` (retention pruning off the
        socket task reaches one the same way a user's own delete does) can complete while this call is
        waiting to enter the gate, and a stale "yes" would then read a just-vacated key as an unsaved
        turn, raising :class:`TurnNotFound` where the caller was promised :class:`ConversationNotFound`.
        ``retitle`` already makes its own "is this still real" check inside its hold for the identical
        reason, which is the precedent this follows.

        The cached agent is dropped, not discarded: see ``AgentRegistry.drop_agent`` for why taking the
        lock with it would be a concurrency bug. It rebuilds from the shortened store on next access, so
        the deleted turns leave the model's context as well as the sidebar.
        """
        async with self._gate.turn(conversation_id):
            if not self.exists(conversation_id):
                raise ConversationNotFound(f"Conversation {conversation_id} is not in the store.")
            if turn_running is not None and turn_running(conversation_id):
                raise TurnInFlight(f"Conversation {conversation_id} has a turn in flight.")
            session = self._store.get(conversation_id)
            if turn_end(session.messages, user_index) is None:
                raise TurnNotFound(f"Conversation {conversation_id} has no user turn at message {user_index}.")
            removed = len(session.messages) - user_index
            session.messages = session.messages[:user_index]
            for key in TURN_KEYED_METADATA:
                kept = _metadata_before(session.metadata, key, user_index)
                if kept:
                    session.metadata[key] = kept
                else:
                    session.metadata.pop(key, None)
            if not any(is_user_turn(message) for message in session.messages):
                session.metadata.pop("title", None)
            self._store.save(session)
            self._registry.drop_agent(conversation_id)
            return removed

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

        Held under this conversation's own turn slot rather than the gate's exclusive hold. The writer
        drains every in-flight turn before it proceeds, so a delete used to wait out a turn on an
        unrelated conversation. The web front end applies controls in arrival order on a single task, so
        that wait queued every other control behind it, ``/stop`` included; the socket is read on a
        separate task, which makes that a delay rather than a wedge (see ``frontends/web.py``), but it
        was still the queueing cost that moved this off the exclusive hold. What the hold has to cover is
        narrower than the writer anyway. This conversation's own turn must be off its agent and message
        list before the store record and the registry entry go, and ``cancel_turn`` above has already
        asked that turn to stop. So the wait is bounded by this conversation's own work: the
        cancellation landing, plus any turn already queued on it, which the per-conversation lock hands
        the slot to first (it is FIFO, so a queued turn is drained ahead of this delete exactly as the
        writer used to drain it). A reader still keeps a settings apply out, since that runs as the
        writer and so cannot overlap one.

        Being one ``gate.turn`` makes this bound by invariant 1 (see ``core/turns.py``): a caller already
        holding a turn must not reach here, which is why ``TurnRunner._prune_task_conversations`` is
        sequenced after its firing's hold rather than inside it.
        """
        cancel_turn(conversation_id)
        deleting_active = conversation_id == self._active_id
        previous_id = self._active_id
        async with self._gate.turn(conversation_id):
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

    async def retitle(self, conversation_id: str, title: str, *, replacing: str) -> bool:
        """Swap ``replacing`` for *title* on a conversation. Returns whether it wrote.

        The write for a generated title (``core/titles.py``), which lands a moment after the
        placeholder ``persist`` derived. Both guards are load-bearing, and each covers a race that
        the seconds-long model call makes ordinary rather than theoretical:

        ``replacing`` is the title this call was built to replace, so whatever is in that slot now
        wins instead. Nothing renames a conversation today, so the case that actually happens is the
        deleted one, and that one is a silent resurrection without this: ``store.get`` answers a
        missing key with a fresh empty ``Session``, so a blind write would re-create the row the
        delete removed, holding a title and nothing else. It is also the guard already standing
        wherever a rename lands later.

        Held under this conversation's own turn slot, like :meth:`delete` and for the same bounded
        reason (see ``core/turn_gate.py``): the store saves whole sessions, so a read-modify-write
        racing the next turn's ``persist`` would revert that turn's messages. Being one
        ``gate.turn`` makes this bound by invariant 1 in ``core/turns.py``, so the caller must not
        already hold a turn: the title task runs outside the turn that spawned it, which is what
        makes this safe *and* what makes it wait for that turn to finish.
        """
        async with self._gate.turn(conversation_id):
            session = self._store.get(conversation_id)
            if session.metadata.get("title") != replacing:
                return False
            session.metadata["title"] = title
            self._store.save(session)
            return True

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
        usage: Optional[dict] = None,
    ) -> None:
        """Persist what produced a turn's output: its sub-agent activity, the model that answered, the
        reasoning effort it ran at, why it stopped early if it did, and what it cost.

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

        ``usage`` is what the turn cost: model calls, tokens, and seconds, as ``core.metrics.TurnMetrics``
        accumulated them. It belongs here for the same reason the model does, and one more: a token
        figure is only meaningful beside the model that produced it, and this is the record that already
        says which model that was. A turn whose provider reported no token counts stores the record
        without them rather than storing zeros, so a reader can tell an unmeasured turn from a free one.
        """
        if user_index < 0 or not (events or model or thinking is not None or failure or usage):
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
        if usage:
            session.metadata.setdefault("usage", {})[str(user_index)] = usage
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
        wanted = _fragment(conversation_id)
        if not wanted:
            return None
        if self.exists(wanted):
            return self.get(wanted)
        if len(wanted) < ID_PREFIX_MIN:
            return None
        matches = [session for session in self.sessions() if session.key.startswith(wanted)]
        return matches[0] if len(matches) == 1 else None

    def matching_ids(self, fragment: str) -> list[str]:
        """Every conversation id starting with *fragment*, for a caller explaining a ``resolve`` refusal.

        Reads the fragment exactly as ``resolve`` does, so the explanation always describes the question
        that was actually asked, and applies no length floor: the point is to report what a fragment
        ``resolve`` already rejected does match.
        """
        wanted = _fragment(fragment)
        if not wanted:
            return []
        return [session.key for session in self.sessions() if session.key.startswith(wanted)]

    def save(self, session: Session) -> None:
        self._store.save(session)

    def touch(self, session: Session) -> None:
        session.metadata["updated_at"] = _now()
        self._store.save(session)
