"""Kokua's browser WebSocket channel: AIMU's ``WebChannel`` plus app-specific frame types.

The generic transport (queue-bridged ``receive()``, streamed ``send()``, the token/thinking/tool/done
frame protocol, and the ``send_frame`` seam) lives in :class:`aimu.aio.channels.web.WebChannel`. This
subclass adds the frames Kokua's richer page needs: a conversation-list sidebar, conversation-history
replay, and tool-call approval prompts. Each is sent through the inherited public ``send_frame``.
"""

from __future__ import annotations

from typing import Any

from aimu.aio.channels.web import WebChannel as BaseWebChannel


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


class WebChannel(BaseWebChannel):
    """AIMU's ``WebChannel`` plus Kokua's conversation-sidebar, history-replay, and approval frames."""

    async def send_conversations(self, items: list[dict]) -> None:
        """Send the conversation list so the page can render the sidebar."""
        await self.send_frame({"type": "conversations", "items": items})

    async def send_history(self, messages: list[dict]) -> None:
        """Send a conversation as one batched frame the page replays (replacing the current view).

        Always sent, even when empty, so switching to a new/empty conversation clears the page.
        """
        items = conversation_to_frames(messages, show_thinking=self.show_thinking, show_tools=self.show_tools)
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
