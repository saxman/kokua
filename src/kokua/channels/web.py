"""A WebSocket Channel adapter bridging one browser onto AIMU's `Channel` ABC.

A server-side pump task feeds inbound text frames into a queue that `receive()` drains, and
`send()` relays a finished string or a streamed reply as JSON frames the static page renders.
Kept separate from the web server (frontends/web.py) so it can be unit-tested in isolation.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional, Union

from aimu.aio.channels.base import Channel, ChannelMessage
from aimu.models import StreamChunk, StreamingContentType


def _text_of(content: Any) -> str:
    """Extract display text from a message's content (a plain string or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def conversation_to_frames(messages: list[dict], *, show_thinking: bool, show_tools: bool) -> list[dict]:
    """Flatten stored conversation messages into ordered display items the page replays on reload.

    Mirrors the live stream order per assistant message: reasoning, then tool calls, then the answer,
    each gated by the same ``show_thinking`` / ``show_tools`` flags the live stream uses. Tool results
    and the system message are omitted (live chat shows neither).
    """
    items: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "user", "text": text})
        elif role == "assistant":
            if show_thinking and message.get("thinking"):
                items.append({"type": "thinking", "text": message["thinking"]})
            if show_tools:
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    items.append({"type": "tool", "name": fn.get("name"), "arguments": fn.get("arguments")})
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "message", "text": text, "proactive": False})
    return items


class WebChannel(Channel):
    """Bridges one browser WebSocket onto the Channel ABC.

    The server's pump task calls `feed(text)` for each inbound frame and `feed(None)` on
    disconnect; `receive()` ends on that sentinel, which lets the Assistant loop tear down cleanly.
    Frames sent to the browser are JSON: ``{"type": "message"|"token"|"thinking"|"tool"|"done", ...}``.
    """

    name = "web"

    def __init__(self, websocket: Any, *, show_thinking: bool = False, show_tools: bool = False):
        # websocket is a Starlette WebSocket (duck-typed: async send_json / close). Tests pass a fake.
        self._ws = websocket
        self._inbound: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._closed = False
        self.show_thinking = show_thinking
        self.show_tools = show_tools

    async def feed(self, text: Optional[str]) -> None:
        """Enqueue an inbound frame; ``None`` is the end-of-stream sentinel."""
        await self._inbound.put(text)

    async def receive(self) -> AsyncIterator[ChannelMessage]:
        while True:
            text = await self._inbound.get()
            if text is None:  # sentinel: the socket closed
                return
            yield ChannelMessage(text=text, sender="web", channel=self.name)

    async def send(
        self,
        content: Union[str, AsyncIterator[StreamChunk]],
        *,
        reply_to: Optional[ChannelMessage] = None,
    ) -> None:
        # A finished string is a single message frame. A proactive push (scheduler) has no
        # reply_to and arrives as a string, so the page can flag it; reactive replies always stream.
        if isinstance(content, str):
            await self._safe_send({"type": "message", "text": content, "proactive": reply_to is None})
            return
        async for chunk in content:
            if chunk.phase == StreamingContentType.GENERATING and chunk.content:
                await self._safe_send({"type": "token", "text": chunk.content})
            elif chunk.phase == StreamingContentType.THINKING and self.show_thinking and chunk.content:
                await self._safe_send({"type": "thinking", "text": chunk.content})
            elif chunk.phase == StreamingContentType.TOOL_CALLING and self.show_tools:
                call = chunk.content if isinstance(chunk.content, dict) else {}
                await self._safe_send({"type": "tool", "name": call.get("name"), "arguments": call.get("arguments")})
        await self._safe_send({"type": "done"})

    async def send_conversations(self, items: list[dict]) -> None:
        """Send the conversation list so the page can render the sidebar."""
        await self._safe_send({"type": "conversations", "items": items})

    async def send_history(self, messages: list[dict]) -> None:
        """Send the prior conversation as one batched frame so the page can render it on reload."""
        items = conversation_to_frames(messages, show_thinking=self.show_thinking, show_tools=self.show_tools)
        if items:
            await self._safe_send({"type": "history", "items": items})

    async def send_approval_request(self, name: str, arguments: Any) -> None:
        """Ask the browser to approve a tool call; the page replies with a normal 'y'/'n' frame.

        The reply flows back through the ordinary inbound path (receive()), so the Assistant's serve
        loop routes it to the pending approval -- no interception is needed here.
        """
        await self._safe_send({"type": "approval", "name": name, "arguments": arguments})

    async def _safe_send(self, frame: dict) -> None:
        # Once closed (e.g. a proactive push racing a disconnect), swallow send errors so a late
        # frame can't crash the scheduler task.
        if self._closed:
            return
        try:
            await self._ws.send_json(frame)
        except Exception:
            self._closed = True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close()
        except Exception:
            pass  # already closed by the client / disconnect
