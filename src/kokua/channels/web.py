"""Kokua's browser WebSocket channel: AIMU's ``WebChannel`` plus app-specific frame types.

The generic transport (queue-bridged ``receive()``, streamed ``send()``, the token/thinking/tool/done
frame protocol, and the ``send_frame`` seam) lives in :class:`aimu.aio.channels.web.WebChannel`. This
subclass adds the frames Kokua's richer page needs: a conversation-list sidebar, conversation-history
replay, and tool-call approval prompts. Each is sent through the inherited public ``send_frame``.

Phase B lets turns on different conversations run concurrently, but only the conversation the user is
currently viewing should stream token/thinking/tool frames; a background turn runs silently and posts a
``notification`` frame on completion instead. The module-level :data:`streaming_conversation` contextvar
carries the running turn's conversation id (set by ``Assistant._handle`` for the task running that turn);
:meth:`WebChannel._foreground` compares it against :attr:`WebChannel.active_conversation_id` (the viewed
conversation) to decide whether to emit. ``None`` means "no turn context" (e.g. a direct push, or the
CLI channel, which has no muting at all) and is always treated as foreground.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
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

from ..images import ROUTE_PREFIX

# The conversation id of the turn currently running in this task (and its awaited children), set by
# Assistant._handle for the duration of the turn and unset (default None) outside any turn. WebChannel
# instances read it to decide whether a turn's frames belong to the conversation being viewed.
streaming_conversation: ContextVar[Optional[str]] = ContextVar("streaming_conversation", default=None)

# User-role turns the agent loop injects between tool-calling iterations. They are byte-for-byte
# ordinary user messages except for this provenance tag, so display keys off the tag alone.
_LOOP_PROVENANCE = frozenset({PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER})


def _text_of(content: Any) -> str:
    """Extract display text from a message's content (a plain string or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


# A stored image reference: our own /images/<name> route, the compacted form persisted in place of inline
# base64 (see images.py / assistant._compact_images). Bounded to a bare filename (no slashes) so the match
# can't run past the reference into surrounding prose.
_IMAGE_REF_RE = re.compile(r"/images/[\w.\-]+")


def _image_frame_for(chunk: StreamChunk) -> Optional[dict]:
    """Return an ``image`` frame for a final IMAGE_GENERATING chunk, else None.

    The chunk's ``result`` is the absolute path the image client wrote (the image toolpack directs it into
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


def conversation_to_frames(
    messages: list[dict],
    *,
    show_thinking: bool,
    show_tools: bool,
    subagent: Optional[dict] = None,
    trace: Optional[dict] = None,
) -> list[dict]:
    """Flatten stored conversation messages into ordered display items the page replays on reload.

    Mirrors the live stream order per assistant message: reasoning, then tool calls, then the answer,
    each gated by the same ``show_thinking`` / ``show_tools`` flags the live stream uses. Tool results
    and the system message are omitted (live chat shows neither).

    Two per-turn maps key a user-message index (as a string) to that turn's recorded reviewer activity,
    interleaved right after the user bubble so it replays in place:
      - ``subagent``: summary verdict cards (non-verbose turns).
      - ``trace``: the full raw verbose trace as ``phase`` + ``reasoning`` items. A traced turn shows
        the raw output instead of cards, and its trace already ends with the final answer, so the
        committed assistant message for that turn is skipped to avoid showing the answer twice.
    """
    subagent = subagent or {}
    trace = trace or {}
    items: list[dict] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        provenance = message.get(PROVENANCE_KEY)
        if role == "user":
            if provenance in _LOOP_PROVENANCE:
                # A framework-injected continuation/final-answer turn, not user input. Show a loop
                # marker carrying the injected prompt text (for inspection), not a user bubble.
                items.append({"type": "loop", "text": _text_of(message.get("content"))})
                continue
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "user", "text": text})
            for url in _image_refs_of(message.get("content")):  # uploaded images, replayed under the bubble
                items.append({"type": "image", "url": url, "from": "user"})
            if str(index) in trace:  # verbose turn: replay the raw trace, not cards
                for segment in trace[str(index)]:
                    items.append(
                        {"type": "phase", "label": segment.get("label", ""), "detail": segment.get("detail", "")}
                    )
                    if segment.get("text"):
                        items.append({"type": "reasoning", "text": segment["text"]})
            else:
                for event in subagent.get(str(index), []):
                    items.append({"type": "subagent", **event})
        elif role == "assistant":
            if str(index - 1) in trace:
                # The preceding user turn was verbose; its trace already contains this final answer
                # (in its last Executor phase), so don't emit it again as a separate message.
                continue
            if show_thinking and message.get("thinking"):
                items.append({"type": "thinking", "text": message["thinking"]})
            if show_tools:
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    items.append({"type": "tool", "name": fn.get("name"), "arguments": fn.get("arguments")})
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "message", "text": text, "proactive": provenance == PROVENANCE_PROACTIVE})
        elif role == "tool":
            # Tool results are otherwise not replayed, but a generate_image result carries an /images/
            # reference the user asked to see, so surface it as an image (regardless of show_tools).
            for url in _image_refs_of(message.get("content")):
                items.append({"type": "image", "url": url, "from": "assistant"})
    return items


class WebChannel(BaseWebChannel):
    """AIMU's ``WebChannel`` plus Kokua's conversation-sidebar, history-replay, and approval frames."""

    def __init__(self, websocket: Any, *, show_thinking: bool = False, show_tools: bool = False):
        super().__init__(websocket, show_thinking=show_thinking, show_tools=show_tools)
        # The conversation this socket is currently viewing (set by the front end, Task 6/7). None
        # until then, which _foreground() treats as "always foreground" (nothing to compare against).
        self.active_conversation_id: Optional[str] = None

    def _foreground(self) -> bool:
        """Whether the running turn's frames belong to the conversation this socket is viewing.

        ``streaming_conversation`` is None outside any turn context (a direct push, or a proactive send
        that isn't wrapped by ``Assistant._handle``), which is always foreground. ``active_conversation_id``
        is None until the front end starts tracking the viewed conversation (Task 6/7); until then there is
        nothing to mute against, so every turn is foreground -- the pre-Phase-B behavior for the one
        connection Kokua allows at a time."""
        if self.active_conversation_id is None:
            return True
        viewing = streaming_conversation.get()
        return viewing is None or viewing == self.active_conversation_id

    async def send_notification(self, text: str) -> None:
        """A background turn finished; tell the user without stealing the current view."""
        await self.send_frame({"type": "notification", "text": text})

    async def feed_input(self, text: str, image_paths: list[str]) -> None:
        """Enqueue a user turn carrying attached image file paths (the web pump's ``input`` frame).

        Plain chat / ``/stop`` / approval replies still arrive through the base string ``feed``; only a
        turn with images uses this richer path, so ``receive`` can populate ``ChannelMessage.images``."""
        await self._inbound.put({"text": text, "images": image_paths})

    async def receive(self) -> AsyncIterator[ChannelMessage]:
        """Yield inbound turns; a dict item carries attached image paths, a string is a plain text turn.

        Overrides the base (string-only) receive so uploaded images reach the agent. ``None`` remains the
        socket-closed sentinel."""
        while True:
            item = await self._inbound.get()
            if item is None:
                return
            if isinstance(item, dict):
                yield ChannelMessage(
                    text=item.get("text", ""), images=item.get("images") or None, sender="web", channel=self.name
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
        hook); strings (including proactive pushes) pass straight through. Muted for a background turn
        (see the module docstring): a string is dropped, and a stream is still fully drained -- so the
        agent run completes and its state persists -- but emits no frames.
        """
        if isinstance(content, str):
            if self._foreground():
                await super().send(content, reply_to=reply_to)
            return
        if not self._foreground():
            async for _ in content:  # drain so the agent run completes, but emit nothing
                pass
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

        Muted for a background turn: still fully drains ``chunks`` and returns the accumulated text (the
        caller needs it regardless of who's watching), but emits no frames.
        """
        from aimu.aio.agent import DEFAULT_CONTINUATION_PROMPT

        foreground = self._foreground()
        parts: list[str] = []
        last_iteration = 0
        async for chunk in chunks:
            if chunk.iteration > last_iteration:
                if foreground:
                    await self.send_frame({"type": "loop", "text": DEFAULT_CONTINUATION_PROMPT})
                last_iteration = chunk.iteration
            if chunk.phase == StreamingContentType.GENERATING:
                if isinstance(chunk.content, str):
                    parts.append(chunk.content)
                    if foreground and show_answer and chunk.content:
                        await self.send_frame({"type": "token", "text": chunk.content})
            elif chunk.phase == StreamingContentType.THINKING and self.show_thinking and chunk.content:
                if foreground:
                    await self.send_frame({"type": "thinking", "text": chunk.content})
            elif chunk.phase == StreamingContentType.TOOL_CALLING and self.show_tools:
                if foreground:
                    call = chunk.content if isinstance(chunk.content, dict) else {}
                    await self.send_frame(
                        {"type": "tool", "name": call.get("name"), "arguments": call.get("arguments")}
                    )
            else:
                if foreground:
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
        (non-verbose turns) and its ``trace`` map replays the raw verbose trace (verbose turns).
        """
        meta = metadata or {}
        items = conversation_to_frames(
            messages,
            show_thinking=self.show_thinking,
            show_tools=self.show_tools,
            subagent=meta.get("subagent"),
            trace=meta.get("trace"),
        )
        await self.send_frame({"type": "history", "items": items})

    async def send_settings(self, values: dict) -> None:
        """Send the current runtime settings so the page can populate its settings panel."""
        await self.send_frame({"type": "settings", "values": values})

    async def send_approval_request(self, name: str, arguments: Any) -> None:
        """Ask the browser to approve a tool call; the page replies with a normal 'y'/'n' frame.

        The reply flows back through the ordinary inbound path (receive()), so the Assistant's serve
        loop routes it to the pending approval -- no interception is needed here.
        """
        await self.send_frame({"type": "approval", "name": name, "arguments": arguments})

    async def send_plan(self, plan: str) -> None:
        """Show a deep-planning plan as its own bubble (rendered as markdown by the page).

        Muted for a background turn (see the module docstring)."""
        if not self._foreground():
            return
        await self.send_frame({"type": "plan", "text": plan})

    async def send_done(self) -> None:
        """Emit a terminal ``done`` frame (verbose trace): finalize the last streamed bubble and clear the
        page's processing state, since the streamed answer isn't followed by a ``message``.

        Muted for a background turn (see the module docstring)."""
        if not self._foreground():
            return
        await self.send_frame({"type": "done"})

    async def send_phase(self, label: str, detail: str = "") -> None:
        """Mark the start of a labeled phase in a verbose planned turn (planner / reviewer / executor).

        The page finalizes any open streaming bubble and starts a fresh one under this header, so each
        LLM call's streamed output reads as its own labeled block. Muted for a background turn (see the
        module docstring)."""
        if not self._foreground():
            return
        await self.send_frame({"type": "phase", "label": label, "detail": detail})

    async def send_subagent(self, event: dict) -> None:
        """Show sub-agent (reviewer) activity as its own card. ``event`` carries an ``id`` (so a
        'running' card updates in place on its verdict), a ``role``, a ``status``, and any ``issues``.

        Muted for a background turn (see the module docstring)."""
        if not self._foreground():
            return
        await self.send_frame({"type": "subagent", **event})

    async def send_plan_review_request(self, plan: str, critique: Optional[str] = None) -> None:
        """Ask the browser to review a plan; the page replies with a normal 'approve'/'reject'/'edit:'
        frame that the serve loop routes to the pending plan (same path as approval). ``critique`` carries
        any adversarial-reviewer concerns for the user to weigh."""
        await self.send_frame({"type": "plan_review", "plan": plan, "critique": critique})
