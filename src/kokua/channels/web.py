"""Kokua's browser WebSocket channel: AIMU's ``WebChannel`` plus app-specific frame types.

The generic transport (queue-bridged ``receive()``, streamed ``send()``, the token/thinking/tool/done
frame protocol, and the ``send_frame`` seam) lives in :class:`aimu.aio.channels.web.WebChannel`. This
subclass adds the frames Kokua's richer page needs: a conversation-list sidebar, conversation-history
replay, and tool-call approval prompts. Each is sent through the inherited public ``send_frame``.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Union

from aimu.aio.channels.base import ChannelMessage
from aimu.aio.channels.web import WebChannel as BaseWebChannel
from aimu.models import (
    PROVENANCE_CONTINUATION,
    PROVENANCE_FINAL_ANSWER,
    PROVENANCE_KEY,
    PROVENANCE_PROACTIVE,
    StreamChunk,
)

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


def conversation_to_frames(
    messages: list[dict], *, show_thinking: bool, show_tools: bool, subagent: Optional[dict] = None
) -> list[dict]:
    """Flatten stored conversation messages into ordered display items the page replays on reload.

    Mirrors the live stream order per assistant message: reasoning, then tool calls, then the answer,
    each gated by the same ``show_thinking`` / ``show_tools`` flags the live stream uses. Tool results
    and the system message are omitted (live chat shows neither). ``subagent`` maps a user-message index
    (as a string) to that turn's recorded sub-agent verdicts, interleaved right after the user bubble so
    reviewer activity replays in place.
    """
    subagent = subagent or {}
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
            for event in subagent.get(str(index), []):
                items.append({"type": "subagent", **event})
        elif role == "assistant":
            if show_thinking and message.get("thinking"):
                items.append({"type": "thinking", "text": message["thinking"]})
            if show_tools:
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    items.append({"type": "tool", "name": fn.get("name"), "arguments": fn.get("arguments")})
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "message", "text": text, "proactive": provenance == PROVENANCE_PROACTIVE})
    return items


class WebChannel(BaseWebChannel):
    """AIMU's ``WebChannel`` plus Kokua's conversation-sidebar, history-replay, and approval frames."""

    async def send(
        self,
        content: Union[str, AsyncIterator[StreamChunk]],
        *,
        reply_to: Optional[ChannelMessage] = None,
    ) -> None:
        """Stream a reply, emitting a ``loop`` marker at each agent-loop iteration boundary.

        Wraps the chunk iterator so the base ``send`` loop is reused unchanged (it has no per-chunk
        hook); strings (including proactive pushes) pass straight through.
        """
        if isinstance(content, str):
            await super().send(content, reply_to=reply_to)
            return
        await super().send(self._mark_loop_boundaries(content), reply_to=reply_to)

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
            yield chunk

    async def send_conversations(self, items: list[dict]) -> None:
        """Send the conversation list so the page can render the sidebar."""
        await self.send_frame({"type": "conversations", "items": items})

    async def send_history(self, messages: list[dict], metadata: Optional[dict] = None) -> None:
        """Send a conversation as one batched frame the page replays (replacing the current view).

        Always sent, even when empty, so switching to a new/empty conversation clears the page.
        ``metadata`` is the active session's metadata; its ``subagent`` map interleaves reviewer cards.
        """
        items = conversation_to_frames(
            messages,
            show_thinking=self.show_thinking,
            show_tools=self.show_tools,
            subagent=(metadata or {}).get("subagent"),
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
        """Show a deep-planning plan as its own bubble (rendered as markdown by the page)."""
        await self.send_frame({"type": "plan", "text": plan})

    async def send_subagent(self, event: dict) -> None:
        """Show sub-agent (reviewer) activity as its own card. ``event`` carries an ``id`` (so a
        'running' card updates in place on its verdict), a ``role``, a ``status``, and any ``issues``."""
        await self.send_frame({"type": "subagent", **event})

    async def send_plan_review_request(self, plan: str, critique: Optional[str] = None) -> None:
        """Ask the browser to review a plan; the page replies with a normal 'approve'/'reject'/'edit:'
        frame that the serve loop routes to the pending plan (same path as approval). ``critique`` carries
        any adversarial-reviewer concerns for the user to weigh."""
        await self.send_frame({"type": "plan_review", "plan": plan, "critique": critique})
