"""Kokua's browser WebSocket channel: AIMU's ``WebChannel`` plus app-specific frame types.

The generic transport (queue-bridged ``receive()``, streamed ``send()``, the token/thinking/tool/done
frame protocol, and the ``send_frame`` seam) lives in :class:`aimu.aio.channels.web.WebChannel`. This
subclass adds the frames Kokua's richer page needs: a conversation-list sidebar, conversation-history
replay, and tool-call approval prompts. Each is sent through the inherited public ``send_frame``.

Turns on different conversations run concurrently, but only the conversation the user is
currently viewing should stream token/thinking/tool frames; a background turn runs silently and posts a
``notification`` frame on completion instead. The module-level :data:`streaming_conversation` contextvar
carries the running turn's conversation id (set by ``Assistant._handle``/``_proactive`` for the task
running that turn); :meth:`WebChannel._foreground` compares it against
:attr:`WebChannel.active_conversation_id` (the viewed conversation) to decide whether to emit. ``None``
means "no turn context" (e.g. a direct push, or the CLI channel, which has no muting at all) and is
always treated as foreground. ``Assistant._approve`` reads the same contextvar (against
``Assistant._active_id`` rather than this channel's ``active_conversation_id``) to gate tool approval on
foreground: a background turn auto-denies a gated tool since no user is watching to confirm it.

**That decision is made per frame, in :meth:`WebChannel.send_frame`, not once per send.** The viewed
conversation changes mid-turn -- that is the whole point of backgrounding one -- so a check hoisted out
of a streaming loop keeps emitting for a turn the user has already switched away from, and the page
appends the rest of that reply to the conversation now on screen. Muting a frame is therefore keyed off
its *type* (:data:`_TURN_FRAMES`) rather than off when the send started.

Muting a turn is not losing it. Every turn frame is also folded into that conversation's
:class:`_CatchUpRecord`, and a switch-in replays the record on the end of the ``history`` frame, so a
conversation whose turn is still running shows the turn so far and then streams the rest live. The core
opens and drops those records (``begin_catch_up`` / ``end_catch_up``), because only it knows when a turn
starts and when its state reaches the store.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union

from aimu.aio.channels.base import ChannelMessage
from aimu.aio.channels.web import WebChannel as BaseWebChannel
from aimu.models import StreamChunk, StreamingContentType

from kokua.images import ROUTE_PREFIX

# The conversation id of the turn currently running in this task (and its awaited children), set by
# Assistant._handle for the duration of the turn and unset (default None) outside any turn. WebChannel
# instances read it to decide whether a turn's frames belong to the conversation being viewed.
streaming_conversation: ContextVar[Optional[str]] = ContextVar("streaming_conversation", default=None)

# True while a proactive/scheduled turn runs in this task (set by Assistant._proactive /
# _run_in_new_session). Unlike streaming_conversation, this does not depend on which conversation is
# viewed: a firing on a channel with no conversation list runs in the viewed conversation and would
# otherwise look foreground, so Assistant._approve reads this to auto-deny a
# gated tool for any proactive turn (nobody is watching to confirm an unattended full-access call).
proactive_turn: ContextVar[bool] = ContextVar("proactive_turn", default=False)

# Frames that belong to a running turn, and so are muted when that turn is not the conversation being
# viewed. Every other frame type describes the channel's own state -- the sidebar, replayed history,
# settings, a background turn's completion notification, a human-decision prompt -- and is sent no
# matter which turn's task emits it. That distinction has to be drawn by frame type rather than by task
# context: `TurnRunner._persist` pushes the conversation list from inside the turn's own task, so a
# background turn's sidebar refresh carries a muted conversation in the contextvar and would be dropped.
_TURN_FRAMES = frozenset({"token", "thinking", "tool", "message", "done", "loop", "image", "plan", "phase", "subagent"})


def _now() -> str:
    """Now, in the ISO form the page's timestamp captions parse (the same form persisted messages use)."""
    return datetime.now().isoformat()


def _image_frame_for(chunk: StreamChunk) -> Optional[dict]:
    """Return an ``image`` frame for a final IMAGE_GENERATING chunk, else None.

    The chunk's ``result`` is the absolute path the image client wrote (the image toolset directs it into
    ``images_path``); the page loads it by its /images/<name> reference."""
    if chunk.phase != StreamingContentType.IMAGE_GENERATING:
        return None
    content = chunk.content if isinstance(chunk.content, dict) else {}
    if not content.get("final"):
        return None
    result = content.get("result")
    if not isinstance(result, str):
        return None
    return {"type": "image", "url": ROUTE_PREFIX + Path(result).name}


class _CatchUpRecord:
    """One in-flight turn's output, kept as the display items a conversation replay would express it as.

    A ``history`` frame replaces the page's whole transcript, so switching into a conversation whose turn
    is still running would otherwise show that turn as though it had never started: its messages are not
    in the store yet (``TurnRunner._persist`` runs at the end of the turn), and the frames that carried
    them either went to a view that has since been replaced or were muted while the user looked
    elsewhere. This is the stand-in, appended to that conversation's history items so the switch-in
    catches up, after which live frames stream into the same bubble.

    Items follow the page's own append rules, so the replay reads like the render it stands in for:
    consecutive thinking text collects into one foldable; answer text collects into a still-open
    ``partial`` bubble that any other block closes, so the prose keeps the place it arrived in and the
    tokens after that block open a new bubble below it (matching what the page renders); anything else
    closes the thinking block; and ``phase``, ``done``, and ``message`` close the answer too, as
    ``finalizeStreaming()`` does on the page.
    """

    def __init__(self):
        self._items: list[dict] = []
        self._thinking: Optional[dict] = None
        self._partial: Optional[dict] = None

    def record_user(self, text: str, image_references: list[str], ts: str) -> None:
        """Open the record with the turn's own user bubble, which the page renders locally on send and
        so is the one item no frame ever carries."""
        if text:
            self._items.append({"type": "user", "text": text, "ts": ts})
        for reference in image_references:
            self._items.append({"type": "image", "url": reference, "from": "user", "ts": ts})

    def record(self, frame: dict, ts: str) -> None:
        """Fold one outgoing display frame into the items, muted or not.

        A muted frame has to be recorded because the user never saw it; a sent one has to be recorded
        too, because the ``history`` frame that arrives on the next switch-in wipes it from the page.
        """
        kind = frame.get("type")
        if kind == "thinking":
            if self._thinking is None:
                self._thinking = {"type": "thinking", "text": "", "ts": ts}
                self._items.append(self._thinking)
            self._thinking["text"] += frame.get("text", "")
            return
        self._thinking = None
        if kind == "token":
            # Anything else the turn emitted since the last token ends the answer segment it interrupted
            # (`self._items[-1]` is that block, not the open bubble): what follows is a new answer below
            # it. Compared by identity, not equality, since two segments can hold the same text.
            if self._partial is not None and self._items[-1] is not self._partial:
                self._close_partial(ts)
            if self._partial is None:
                self._partial = {"type": "partial", "text": ""}  # unstamped: it is still being written
                self._items.append(self._partial)
            self._partial["text"] += frame.get("text", "")
            return
        if kind in ("phase", "done", "message"):
            self._close_partial(ts)
        if kind == "done":
            return  # a terminator has no rendered form to replay
        item = {**frame, "ts": ts}
        if kind == "image":
            item["from"] = "assistant"  # a live image frame is always generated; replay aligns on this
        self._items.append(item)

    def _close_partial(self, ts: str) -> None:
        """Finish the open answer segment, in place, as the finished bubble it now is.

        A closed segment replays as a ``message`` rather than staying a ``partial``: it wants the markdown
        render and the timestamp the page gives a bubble at ``finalizeStreaming()``, and leaving it a
        ``partial`` would also make the replay re-open it as the bubble live tokens stream into, so the
        next segment's text would land in the wrong one.
        """
        if self._partial is None:
            return
        self._partial["type"] = "message"
        self._partial["ts"] = ts
        self._partial = None

    def items(self) -> list[dict]:
        """The recorded items, copied so a replay cannot be mutated by the turn still running."""
        return [dict(item) for item in self._items]


class WebChannel(BaseWebChannel):
    """AIMU's ``WebChannel`` plus Kokua's conversation-sidebar, history-replay, and approval frames."""

    def __init__(self, websocket: Any):
        super().__init__(websocket)
        # The conversation this socket is currently viewing (set by the front end). None until then,
        # which _foreground() treats as "always foreground" (nothing to compare against).
        self.active_conversation_id: Optional[str] = None
        # Per-conversation catch-up records for the turns in flight right now, opened and dropped by the
        # core (begin_catch_up / end_catch_up). A conversation with no running turn has no entry.
        self._catch_up: dict[str, _CatchUpRecord] = {}

    def begin_catch_up(self, conversation_id: str, text: str, image_paths: Optional[list[str]] = None) -> None:
        """Start recording a turn's output, so switching into its conversation mid-turn shows the turn.

        Replaces any previous record for the conversation: one turn runs per conversation at a time, so
        an older record can only be a leftover from a turn that failed before it persisted.
        """
        record = _CatchUpRecord()
        record.record_user(text, [ROUTE_PREFIX + Path(path).name for path in image_paths or []], _now())
        self._catch_up[conversation_id] = record

    def end_catch_up(self, conversation_id: str) -> None:
        """Drop a conversation's record, once the store holds what it stood in for (or the turn is over
        without ever getting there). Idempotent, since the core calls it on both of those events."""
        self._catch_up.pop(conversation_id, None)

    def _foreground(self) -> bool:
        """Whether the running turn's frames belong to the conversation this socket is viewing.

        ``streaming_conversation`` is None outside any turn context (a direct push, or a proactive send
        that isn't wrapped by ``Assistant._handle``), which is always foreground. ``active_conversation_id``
        is None until the front end starts tracking the viewed conversation; until then there is nothing
        to mute against, so every turn is foreground -- correct for the one connection Kokua allows at a
        time."""
        if self.active_conversation_id is None:
            return True
        viewing = streaming_conversation.get()
        return viewing is None or viewing == self.active_conversation_id

    async def send_frame(self, frame: dict) -> None:
        """Send ``frame`` unless it belongs to a turn the user isn't watching, or stands in for a card.

        Every live frame passes through here -- the base class's ``send()`` and this class's own
        ``stream_activity()`` both map chunks to frames by calling this inherited method -- which is
        what makes it the one place both the muting rule (:data:`_TURN_FRAMES`, re-evaluated per frame
        so a switch mid-reply takes effect immediately) and the ``spawn_subagent`` suppression (see
        SPAWN_SUBAGENT_TOOL_NAME) can live without duplicating the chunk-to-frame mapping. Replay is
        handled separately, by :func:`kokua.core.transcripts.replay_items`, since a stored turn's tool
        calls reach the browser batched inside one ``history`` frame rather than through here.
        """
        # Imported here, not at module level: kokua.core's __init__ imports assistant.py, which
        # imports this module (for streaming_conversation/proactive_turn, above) before this module
        # has finished loading, so a top-level `from kokua.core.transcripts import ...` back from here
        # would be circular whenever this module is what starts the import chain.
        from kokua.core.transcripts import SPAWN_SUBAGENT_TOOL_NAME

        frame_type = frame.get("type")
        if frame_type == "tool" and frame.get("name") == SPAWN_SUBAGENT_TOOL_NAME:
            return
        if frame_type in _TURN_FRAMES:
            record = self._catch_up.get(streaming_conversation.get())
            if record is not None:
                record.record(frame, _now())
            if not self._foreground():
                return
        await super().send_frame(frame)

    async def send_notification(self, text: str) -> None:
        """A background turn finished; tell the user without stealing the current view."""
        await self.send_frame({"type": "notification", "text": text})

    async def send_working(self, active: bool) -> None:
        """Tell the page whether the conversation it is now viewing has a turn already running in
        the background (started before this switch). Never muted (``working`` is not in
        ``_TURN_FRAMES``): this describes the viewed conversation's own state, not a running turn's
        streamed content, so it is sent regardless of what ``streaming_conversation`` names."""
        await self.send_frame({"type": "working", "active": active})

    async def feed_input(self, text: str, image_paths: list[str], thinking: Optional[str] = None) -> None:
        """Enqueue a user turn carrying attached image file paths, a per-turn reasoning effort, or both
        (the web pump's ``input`` frame).

        Plain chat / ``/stop`` / approval replies still arrive through the base string ``feed``; only a
        turn carrying something besides its text uses this richer path, so ``receive`` can populate
        ``ChannelMessage.images`` and ``ChannelMessage.metadata``."""
        await self._inbound.put({"text": text, "images": image_paths, "thinking": thinking})

    async def receive(self) -> AsyncIterator[ChannelMessage]:
        """Yield inbound turns; a dict item carries attached image paths, a per-turn reasoning effort, or
        both, a string is a plain text turn.

        Overrides the base (string-only) receive so uploaded images reach the agent. ``None`` remains the
        socket-closed sentinel."""
        while True:
            item = await self._inbound.get()
            if item is None:
                return
            if isinstance(item, dict):
                # An absent effort leaves `metadata` empty rather than carrying a None: the core reads a
                # missing key as "use the configured effort", and a present-but-None key would be a
                # second spelling of the same thing for every reader to remember.
                metadata = {} if item.get("thinking") is None else {"thinking": item["thinking"]}
                yield ChannelMessage(
                    text=item.get("text", ""),
                    images=item.get("images") or None,
                    sender="web",
                    channel=self.name,
                    metadata=metadata,
                )
            else:
                yield ChannelMessage(text=item, sender="web", channel=self.name)

    async def send(
        self,
        content: Union[str, AsyncIterator[StreamChunk]],
        *,
        reply_to: Optional[ChannelMessage] = None,
    ) -> None:
        """Stream a reply, forwarding an ``image`` frame for each image-progress chunk along the way.

        Wraps the chunk iterator so the base ``send`` loop is reused unchanged (it has no per-chunk
        hook for image progress); strings (including proactive pushes) pass straight through. A
        background turn needs no special path here: the base loop drains the whole stream either way --
        so the agent run completes and its state persists -- and each frame it produces is muted or not
        on its own, as the view stands when that frame is sent (see the module docstring).
        """
        if isinstance(content, str):
            await super().send(content, reply_to=reply_to)
            return
        await super().send(self._forward_image_frames(content), reply_to=reply_to)

    async def stream_activity(self, chunks: AsyncIterator[StreamChunk], *, show_answer: bool = False) -> str:
        """Stream the agentic loop live and return the accumulated GENERATING text.

        Mirrors the base ``send()`` per-chunk mapping (thinking / tool / loop-boundary frames) but emits
        no ``done`` terminator, so the turn keeps its processing state (``/stop`` still works) until the
        caller sends the final frame. ``GENERATING`` is withheld by default (the caller shows the returned text once it's ready -- a reviewed answer or a
        plan bubble); with ``show_answer=True`` it is also streamed as ``token`` frames (verbose trace,
        where every version and each reviewer's prose is shown live).

        Muted for a background turn, per frame as it is sent (see the module docstring), so a switch
        mid-stream stops the rest. ``chunks`` is fully drained and the accumulated text returned either
        way: the caller needs it regardless of who's watching.
        """
        parts: list[str] = []
        async for chunk in chunks:
            if chunk.phase == StreamingContentType.GENERATING:
                if isinstance(chunk.content, str):
                    parts.append(chunk.content)
                    if show_answer and chunk.content:
                        await self.send_frame({"type": "token", "text": chunk.content})
            elif chunk.phase == StreamingContentType.THINKING and chunk.content:
                await self.send_frame({"type": "thinking", "text": chunk.content})
            elif chunk.phase == StreamingContentType.TOOL_CALLING:
                call = chunk.content if isinstance(chunk.content, dict) else {}
                await self.send_frame(
                    {
                        "type": "tool",
                        "name": call.get("name"),
                        "arguments": call.get("arguments"),
                        "response": call.get("response"),
                    }
                )
            elif chunk.phase == StreamingContentType.CONTINUING:
                call = chunk.content if isinstance(chunk.content, dict) else {}
                await self.send_frame({"type": "loop", "reason": call.get("kind"), "text": call.get("prompt", "")})
            else:
                image = _image_frame_for(chunk)
                if image is not None:
                    await self.send_frame(image)
        return "".join(parts)

    async def _forward_image_frames(self, chunks: AsyncIterator[StreamChunk]) -> AsyncIterator[StreamChunk]:
        """Yield ``chunks`` unchanged, sending an ``image`` frame for each image-progress chunk.

        The base ``send`` loop has no branch for image progress and no per-chunk hook, so this wrapper
        is how those frames reach the page. Loop boundaries used to be inferred here from a rise in
        ``StreamChunk.iteration``; AIMU now yields a ``CONTINUING`` chunk carrying the injected prompt
        itself, which the base loop maps, so nothing is guessed on this path any more.
        """
        async for chunk in chunks:
            image = _image_frame_for(chunk)
            if image is not None:
                await self.send_frame(image)
            yield chunk

    async def send_conversations(self, items: list[dict]) -> None:
        """Send the conversation list so the page can render the sidebar."""
        await self.send_frame({"type": "conversations", "items": items})

    async def send_history(self, messages: list[dict], metadata: Optional[dict] = None) -> None:
        """Send a conversation as one batched frame the page replays (replacing the current view).

        Always sent, even when empty, so switching to a new/empty conversation clears the page.
        ``metadata`` is the active session's metadata; its ``subagent`` map interleaves reviewer cards
        (non-verbose turns), its ``trace`` map replays the raw verbose trace (verbose turns), and its
        ``failure`` map closes a turn that ended in an error with the reason.

        A turn in flight on this conversation contributes its catch-up items (see
        :class:`_CatchUpRecord`) on the end of the same frame. One frame rather than a replay of separate
        frames is what makes that safe: a live frame from the running turn can land between two awaits,
        which would both misorder the catch-up and duplicate the frame it interleaved with.
        """
        # Imported here, not at module level: the same cycle as send_frame's SPAWN_SUBAGENT_TOOL_NAME
        # import above applies to any top-level import back from kokua.core.transcripts.
        from kokua.core.transcripts import replay_items

        meta = metadata or {}
        items = replay_items(
            messages,
            subagent=meta.get("subagent"),
            trace=meta.get("trace"),
            failure=meta.get("failure"),
        )
        record = self._catch_up.get(self.active_conversation_id)
        if record is not None:
            items.extend(record.items())
        await self.send_frame({"type": "history", "items": items})

    async def send_settings(self, values: dict) -> None:
        """Send the current runtime settings, for a client that renders them.

        Kokua's own page does not: it has no settings window, and its theme button is a per-browser
        preference that never reaches the server. The frame stays part of the transport's contract.
        """
        await self.send_frame({"type": "settings", "values": values})

    async def send_tasks(self, items: list[dict]) -> None:
        """Send the scheduled tasks so the page can populate its sidebar task section.

        Like ``send_settings``, this is a front-end concern rather than part of ``RichChannel``: the
        core never sends it, so there is no capability for ``ChannelUI`` to degrade.
        """
        await self.send_frame({"type": "tasks", "items": items})

    async def send_download(self, name: str, url: str) -> None:
        """Point the page at a file to download.

        Like ``send_settings`` and ``send_tasks``, a front-end concern rather than part of
        ``RichChannel``: the core never sends it, so there is no capability for ``ChannelUI`` to
        degrade. The file is already written under ``downloads_path``, which the front end's
        existing ``/download/{name}`` route serves with its own traversal guard, so this frame
        carries a name and not bytes.
        """
        await self.send_frame({"type": "download", "name": name, "url": url})

    async def send_approval_request(self, name: str, arguments: Any) -> None:
        """Ask the browser to approve a tool call; the page replies with a normal 'y'/'n' frame.

        The reply flows back through the ordinary inbound path (receive()), so the Assistant's serve
        loop routes it to the pending approval -- no interception is needed here.
        """
        await self.send_frame({"type": "approval", "name": name, "arguments": arguments})

    async def send_plan(self, plan: str) -> None:
        """Show a deep-planning plan as its own bubble (rendered as markdown by the page).

        Muted for a background turn (see the module docstring)."""
        await self.send_frame({"type": "plan", "text": plan})

    async def send_done(self) -> None:
        """Emit a terminal ``done`` frame (verbose trace): finalize the last streamed bubble and clear the
        page's processing state, since the streamed answer isn't followed by a ``message``.

        Muted for a background turn (see the module docstring)."""
        await self.send_frame({"type": "done"})

    async def send_phase(self, label: str, detail: str = "") -> None:
        """Mark the start of a labeled phase in a verbose planned turn (planner / reviewer / executor).

        The page finalizes any open streaming bubble and starts a fresh one under this header, so each
        LLM call's streamed output reads as its own labeled block. Muted for a background turn (see the
        module docstring)."""
        await self.send_frame({"type": "phase", "label": label, "detail": detail})

    async def send_subagent(self, event: dict) -> None:
        """Show one ``id``-keyed foldable card of sub-agent-style activity, updated in place.

        Two producers share this frame type, told apart by ``task`` (present on a spawn's create
        event, and on every later event of its lineage) versus its absence (a planning reviewer's
        verdict card): a spawn's create event carries ``id``/``role``/``task``/``status: "running"``
        and grows with ``{"id", "append": {"kind": "reasoning" | "tool" | "answer" | "error", ...}}``
        entries (a ``"tool"`` entry carrying ``name``/``arguments``/``response``, the last being what the
        call returned), closing with a terminal ``status`` of ``"done"``, ``"stopped"``, or ``"error"``; a
        reviewer's card carries ``id``/``role``/``status``/``issues`` instead, sent once per round with
        no ``append``, closing with a terminal ``status`` of ``"approved"`` or ``"rejected"``.

        ``reasoning`` and ``answer`` entries each carry one chunk of streamed text; the page appends
        each into the block currently open for that kind, and an entry of another kind closes it. The
        terminal event repeats no already-streamed text, and carries the answer only when a provider
        streamed none.

        Muted for a background turn (see the module docstring)."""
        await self.send_frame({"type": "subagent", **event})

    async def send_plan_review_request(self, plan: str, critique: Optional[str] = None) -> None:
        """Ask the browser to review a plan; the page replies with a normal 'approve'/'reject'/'edit:'
        frame that the serve loop routes to the pending plan (same path as approval). ``critique`` carries
        any adversarial-reviewer concerns for the user to weigh."""
        await self.send_frame({"type": "plan_review", "plan": plan, "critique": critique})
