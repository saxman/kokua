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

import re
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union

from aimu.aio.channels.base import ChannelMessage
from aimu.aio.channels.web import WebChannel as BaseWebChannel
from aimu.models import (
    PROVENANCE_CONTINUATION,
    PROVENANCE_FINAL_ANSWER,
    PROVENANCE_KEY,
    PROVENANCE_PROACTIVE,
    StreamChunk,
    StreamingContentType,
)

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

# User-role turns the agent loop injects between tool-calling iterations. They are byte-for-byte
# ordinary user messages except for this provenance tag, so display keys off the tag alone.
_LOOP_PROVENANCE = frozenset({PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER})

# AIMU's make_async_subagent_tool (aimu/aio/tools/builtin.py) defaults its built tool's name to this
# literal; kokua never overrides it. A spawn's own `subagent` card already shows its role, task, and
# result, so the parent's `tool` frame for this one tool name is pure duplication and is suppressed
# wherever a tool call becomes a display frame: send_frame() below (both live streaming paths route
# through it) and conversation_to_frames()'s replay of a stored message's tool_calls. build.py imports
# this same constant to find and replace the tool on a runtime rebuild.
SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"

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


def _text_of(content: Any) -> str:
    """Extract display text from a message's content (a plain string or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


# A stored image reference: our own /images/<name> route, the compacted form persisted in place of inline
# base64 (see images.py / messages.compact_message_images). Bounded to a bare filename (no slashes) so the match
# can't run past the reference into surrounding prose.
_IMAGE_REF_RE = re.compile(r"/images/[\w.\-]+")


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


def _image_refs_of(content: Any) -> list[str]:
    """Return the image references in a message's content: image_url block urls plus any /images/ refs in text."""
    refs: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url")
                if url:
                    refs.append(url)
    refs.extend(_IMAGE_REF_RE.findall(_text_of(content)))
    return list(dict.fromkeys(refs))  # de-dupe, preserving order


def _tool_results_by_call_id(messages: list[dict]) -> dict[str, str]:
    """Map each tool result in ``messages`` to the id of the call it answers.

    A live ``tool`` frame carries the call and its result together (AIMU emits ``TOOL_CALLING`` only once
    the call has returned), but a stored transcript splits them across an assistant message's
    ``tool_calls`` and a later ``role: "tool"`` message, joined by id. Concurrent dispatch appends those
    results in completion order, so the join has to be by id and not by position.
    """
    return {
        message["tool_call_id"]: str(message.get("content", ""))
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }


def conversation_to_frames(
    messages: list[dict],
    *,
    show_thinking: bool,
    show_tools: bool,
    subagent: Optional[dict] = None,
    trace: Optional[dict] = None,
    failure: Optional[dict] = None,
) -> list[dict]:
    """Flatten stored conversation messages into ordered display items the page replays on reload.

    Mirrors the live stream order per assistant message: reasoning, then the answer, then the tool calls,
    each gated by the same ``show_thinking`` / ``show_tools`` flags the live stream uses. That is the
    order the message was written in -- a model emits its prose and then the calls it decided to make --
    so a reload leaves a turn where the user watched it arrive instead of sinking its prose below the
    cards. The system message is omitted (live chat shows none).

    A ``role: "tool"`` message is not replayed as an item of its own, but its content is rejoined to the
    call it answers (see :func:`_tool_results_by_call_id`) and rides that call's item as ``response``, so
    a replayed tool card carries the output a live one showed. ``response`` is ``None`` where no result
    message exists -- a transcript stored before results were replayed, or a turn cut short mid-dispatch
    -- and the page then renders the card exactly as it always did.

    Two per-turn maps key a user-message index (as a string) to that turn's recorded reviewer activity,
    interleaved right after the user bubble so it replays in place:
      - ``subagent``: summary verdict cards (non-verbose turns). A verbose turn's plan reviewers show
        up in ``trace`` instead, but its executor can still spawn its own sub-agents, and those cards
        (identified by ``task`` on the create event or ``append`` on later ones) replay regardless.
      - ``trace``: the full raw verbose trace as ``phase`` + ``reasoning`` items. A traced turn shows
        the raw output instead of reviewer cards, and its trace already ends with the final answer, so
        the committed assistant message for that turn is skipped to avoid showing the answer twice.

    ``failure`` is keyed the same way, but replays at the *end* of its turn rather than after the user
    bubble: it says why the turn stopped, which only reads correctly after whatever the turn managed to
    produce. It matters most for a scheduled run, whose error never reached this conversation live -- the
    status line for a firing goes to whichever conversation the user was viewing at the time.
    """
    subagent = subagent or {}
    trace = trace or {}
    failure = failure or {}
    items: list[dict] = []
    results = _tool_results_by_call_id(messages)
    pending_failure: Optional[tuple[str, object]] = None  # (reason, the turn's timestamp)

    def add(item: dict, timestamp) -> None:
        # Attach the source message's append-time timestamp (AIMU's inert ``timestamp`` key) so the page
        # can caption the bubble. Omitted when absent (messages persisted before timestamping shipped),
        # so those simply render no caption. Metadata-derived items (phase/reasoning/subagent) pass the
        # turn's user-message timestamp, since they have no timestamp of their own.
        if timestamp:
            item["ts"] = timestamp
        items.append(item)

    def flush_failure() -> None:
        """Close the turn in progress with its recorded reason, if it had one.

        Held until the turn ends rather than emitted where it is read, because the reason belongs after
        the output it cut short. A turn ends at the next user message or at the end of the transcript,
        so this is called from both places; a conversation the user carried on in after a failed turn
        therefore keeps the notice inside that turn instead of trailing it off the bottom.
        """
        nonlocal pending_failure
        if pending_failure is not None:
            reason, turn_ts = pending_failure
            pending_failure = None
            add({"type": "notice", "text": reason}, turn_ts)

    for index, message in enumerate(messages):
        role = message.get("role")
        provenance = message.get(PROVENANCE_KEY)
        ts = message.get("timestamp")
        if role == "user":
            if provenance in _LOOP_PROVENANCE:
                # A framework-injected continuation/final-answer turn, not user input. Show a loop
                # marker carrying the injected prompt text (for inspection), not a user bubble. It
                # continues the turn already in progress rather than starting a new one, so it must not
                # close that turn's failure notice either.
                add({"type": "loop", "text": _text_of(message.get("content"))}, ts)
                continue
            flush_failure()  # whatever turn was in progress ends where this one begins
            if str(index) in failure:
                pending_failure = (failure[str(index)], ts)
            text = _text_of(message.get("content"))
            if text:
                add({"type": "user", "text": text}, ts)
            for url in _image_refs_of(message.get("content")):  # uploaded images, replayed under the bubble
                add({"type": "image", "url": url, "from": "user"}, ts)
            events = subagent.get(str(index), [])
            if str(index) in trace:  # verbose turn: replay the raw trace, not the verdict cards
                for segment in trace[str(index)]:
                    add({"type": "phase", "label": segment.get("label", ""), "detail": segment.get("detail", "")}, ts)
                    if segment.get("text"):
                        add({"type": "reasoning", "text": segment["text"]}, ts)
                # A reviewer's verdict is already in the trace, but a sub-agent the turn spawned is
                # not, so those cards are replayed on their own. A spawn is identified by the `task` on
                # its create event and the rest of its lineage by that event's id: shape alone is not
                # enough, since a spawn whose text streamed closes with a status-only event that looks
                # exactly like a reviewer's, and dropping it strands the card at "working...".
                spawned = {event["id"] for event in events if "task" in event and "id" in event}
                events = [event for event in events if event.get("id") in spawned]
            for event in events:
                add({"type": "subagent", **event}, ts)
        elif role == "assistant":
            if str(index - 1) in trace:
                # The preceding user turn was verbose; its trace already contains this final answer
                # (in its last Executor phase), so don't emit it again as a separate message.
                continue
            if show_thinking and message.get("thinking"):
                add({"type": "thinking", "text": message["thinking"]}, ts)
            text = _text_of(message.get("content"))
            if text:
                add({"type": "message", "text": text, "proactive": provenance == PROVENANCE_PROACTIVE}, ts)
            if show_tools:
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    name = fn.get("name")
                    if name == SPAWN_SUBAGENT_TOOL_NAME:
                        continue  # shown as its own subagent card instead; see SPAWN_SUBAGENT_TOOL_NAME
                    add(
                        {
                            "type": "tool",
                            "name": name,
                            "arguments": fn.get("arguments"),
                            "response": results.get(call.get("id")),
                        },
                        ts,
                    )
        elif role == "tool":
            # Tool results are otherwise not replayed, but a generate_image result carries an /images/
            # reference the user asked to see, so surface it as an image (regardless of show_tools).
            for url in _image_refs_of(message.get("content")):
                add({"type": "image", "url": url, "from": "assistant"}, ts)
    flush_failure()  # the last turn ends at the end of the transcript
    return items


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

    def __init__(self, websocket: Any, *, show_thinking: bool = False, show_tools: bool = False):
        super().__init__(websocket, show_thinking=show_thinking, show_tools=show_tools)
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
        handled separately, in ``conversation_to_frames``, since a stored turn's tool calls reach the
        browser batched inside one ``history`` frame rather than through here.
        """
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
        """Stream a reply, emitting a ``loop`` marker at each agent-loop iteration boundary.

        Wraps the chunk iterator so the base ``send`` loop is reused unchanged (it has no per-chunk
        hook); strings (including proactive pushes) pass straight through. A background turn needs no
        special path here: the base loop drains the whole stream either way -- so the agent run
        completes and its state persists -- and each frame it produces is muted or not on its own, as
        the view stands when that frame is sent (see the module docstring).
        """
        if isinstance(content, str):
            await super().send(content, reply_to=reply_to)
            return
        await super().send(self._mark_loop_boundaries(content), reply_to=reply_to)

    async def stream_activity(self, chunks: AsyncIterator[StreamChunk], *, show_answer: bool = False) -> str:
        """Stream the agentic loop live and return the accumulated GENERATING text.

        Mirrors the base ``send()`` per-chunk mapping (thinking / tool / loop-boundary frames, gated by
        ``show_thinking`` / ``show_tools``) but emits no ``done`` terminator, so the turn keeps its
        processing state (``/stop`` still works) until the caller sends the final frame. ``GENERATING`` is
        withheld by default (the caller shows the returned text once it's ready -- a reviewed answer or a
        plan bubble); with ``show_answer=True`` it is also streamed as ``token`` frames (verbose trace,
        where every version and each reviewer's prose is shown live).

        Muted for a background turn, per frame as it is sent (see the module docstring), so a switch
        mid-stream stops the rest. ``chunks`` is fully drained and the accumulated text returned either
        way: the caller needs it regardless of who's watching.
        """
        from aimu.aio.agent import DEFAULT_CONTINUATION_PROMPT

        parts: list[str] = []
        last_iteration = 0
        async for chunk in chunks:
            if chunk.iteration > last_iteration:
                await self.send_frame({"type": "loop", "text": DEFAULT_CONTINUATION_PROMPT})
                last_iteration = chunk.iteration
            if chunk.phase == StreamingContentType.GENERATING:
                if isinstance(chunk.content, str):
                    parts.append(chunk.content)
                    if show_answer and chunk.content:
                        await self.send_frame({"type": "token", "text": chunk.content})
            elif chunk.phase == StreamingContentType.THINKING and self.show_thinking and chunk.content:
                await self.send_frame({"type": "thinking", "text": chunk.content})
            elif chunk.phase == StreamingContentType.TOOL_CALLING and self.show_tools:
                call = chunk.content if isinstance(chunk.content, dict) else {}
                await self.send_frame(
                    {
                        "type": "tool",
                        "name": call.get("name"),
                        "arguments": call.get("arguments"),
                        "response": call.get("response"),
                    }
                )
            else:
                image = _image_frame_for(chunk)
                if image is not None:
                    await self.send_frame(image)
        return "".join(parts)

    async def _mark_loop_boundaries(self, chunks: AsyncIterator[StreamChunk]) -> AsyncIterator[StreamChunk]:
        """Yield ``chunks`` unchanged, emitting a ``loop`` frame just before each iteration increment.

        ``StreamChunk.iteration`` is 0 for the first response and rises by one per agent-loop
        continuation, so a rise marks the boundary the injected turn sits at. The chunk carries the
        iteration number but not the injected prompt text, so the marker shows the default continuation
        prompt (kokua never overrides ``continuation_prompt``); replay reads the actual stored content.
        """
        from aimu.aio.agent import DEFAULT_CONTINUATION_PROMPT

        last_iteration = 0
        async for chunk in chunks:
            if chunk.iteration > last_iteration:
                await self.send_frame({"type": "loop", "text": DEFAULT_CONTINUATION_PROMPT})
                last_iteration = chunk.iteration
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
        meta = metadata or {}
        items = conversation_to_frames(
            messages,
            show_thinking=self.show_thinking,
            show_tools=self.show_tools,
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
