"""The core's view of a channel: the base contract plus every optional frame, degradation resolved.

``Assistant`` and ``PlanRunner`` talk to a ``ChannelUI``, never to a channel directly. Each optional
frame in :class:`kokua.channels.protocol.RichChannel` has exactly one documented degradation here (a
plain ``send()`` line, or a no-op), decided once at construction rather than at each of the seventeen
call sites that used to ask ``getattr(channel, "send_x", None)`` for themselves.

Three capabilities are exposed as named booleans rather than hidden behind a fallback, because they
are not "call it if present" -- they change what the core *does*:

``supports_conversations``
    Whether a scheduled task may run in its own conversation. Without a sidebar the user would have
    no way to reach it, so such a task runs in the viewed conversation instead.
``supports_phases``
    Whether a planned turn can show its work as a verbose trace, or must fall back to summary cards.
``supports_streamed_activity``
    Whether an intermediate agent call can stream live, or must run non-streaming.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Union

from aimu.aio import Channel
from aimu.aio.channels.base import ChannelMessage
from aimu.models import StreamChunk


def _bullets(issues: list[str]) -> str:
    return "\n".join(f"- {issue}" for issue in issues)


class ChannelUI:
    """Adapts any ``aimu.aio.Channel`` to the full surface the core wants to use."""

    def __init__(self, channel: Channel):
        self._channel = channel
        self._conversations = getattr(channel, "send_conversations", None)
        self._notification = getattr(channel, "send_notification", None)
        self._approval_request = getattr(channel, "send_approval_request", None)
        self._plan_review_request = getattr(channel, "send_plan_review_request", None)
        self._plan = getattr(channel, "send_plan", None)
        self._phase = getattr(channel, "send_phase", None)
        self._done = getattr(channel, "send_done", None)
        self._subagent = getattr(channel, "send_subagent", None)
        self._stream_activity = getattr(channel, "stream_activity", None)

    @property
    def channel(self) -> Channel:
        """The underlying channel, for the front end that constructed it and knows its real type."""
        return self._channel

    # --- capabilities that change what the core does ------------------------------------------

    @property
    def supports_conversations(self) -> bool:
        return self._conversations is not None

    @property
    def supports_phases(self) -> bool:
        return self._phase is not None

    @property
    def supports_streamed_activity(self) -> bool:
        return self._stream_activity is not None

    # --- the base contract ---------------------------------------------------------------------

    async def send(
        self,
        content: Union[str, AsyncIterator[StreamChunk]],
        *,
        reply_to: Optional[ChannelMessage] = None,
    ) -> None:
        await self._channel.send(content, reply_to=reply_to)

    def receive(self) -> AsyncIterator[ChannelMessage]:
        return self._channel.receive()

    # --- optional frames, each with one documented degradation ---------------------------------

    async def push_conversations(self, items: list[dict]) -> None:
        """Refresh the conversation list. Silently skipped by a channel with no sidebar."""
        if self._conversations is not None:
            await self._conversations(items)

    async def notify(self, text: str) -> None:
        """Report a background turn's completion. Skipped by a channel that cannot background a turn."""
        if self._notification is not None:
            await self._notification(text)

    async def ask_approval(self, name: str, arguments: Any) -> None:
        """Prompt for tool approval, as a frame or as a plain-text question."""
        if self._approval_request is not None:
            await self._approval_request(name, arguments)
        else:
            await self._channel.send(f"[approve] Allow {name}({arguments})? [y/N]")

    async def ask_plan_review(self, plan: str, critique: Optional[list[str]] = None) -> None:
        """Prompt for plan approve/edit/reject, surfacing any reviewer critique for the user to weigh."""
        concerns = _bullets(critique) if critique else None
        if self._plan_review_request is not None:
            await self._plan_review_request(plan, concerns)
        else:
            note = ("\nReviewer's concerns:\n" + concerns) if concerns else ""
            await self._channel.send("[plan] Reply 'approve', 'reject', or 'edit: <revised plan>'." + note)

    async def show_plan(self, plan: str) -> None:
        """Show a plan as its own bubble, or as a labeled chat message."""
        if self._plan is not None:
            await self._plan(plan)
        else:
            await self._channel.send(f"Plan:\n\n{plan}")

    async def show_phase(self, label: str, detail: str = "") -> None:
        """Open a labeled phase block. A no-op where phases aren't rendered."""
        if self._phase is not None:
            await self._phase(label, detail)

    async def finish_stream(self) -> None:
        """Terminate a streamed sequence not followed by a message. A no-op where it isn't needed."""
        if self._done is not None:
            await self._done()

    async def show_subagent(self, event: dict) -> None:
        """Show a reviewer activity card. A no-op where cards aren't rendered."""
        if self._subagent is not None:
            await self._subagent(event)

    async def stream_activity(self, chunks: AsyncIterator[StreamChunk], *, show_answer: bool = False) -> str:
        """Stream an agentic loop live and return its generated text.

        Where live streaming isn't available this still drains ``chunks``, so the agent run completes,
        and returns "". A caller that needs the text (rather than just the completion) must check
        ``supports_streamed_activity`` and run non-streaming instead.
        """
        if self._stream_activity is not None:
            return await self._stream_activity(chunks, show_answer=show_answer)
        async for _ in chunks:
            pass
        return ""

    # --- mirrored attributes -------------------------------------------------------------------

    def set_active_conversation(self, conversation_id: str) -> None:
        """Mirror the viewed conversation onto a channel that tracks one, so its muting of background
        frames and the core's background-completion notification agree on what is in view."""
        if hasattr(self._channel, "active_conversation_id"):
            self._channel.active_conversation_id = conversation_id

    def display_flag(self, name: str, default: bool) -> bool:
        """A display flag's effective value: the channel's copy wins, since that is the one consulted
        while streaming."""
        return getattr(self._channel, name, default)

    def set_display_flag(self, name: str, value: bool) -> None:
        if hasattr(self._channel, name):
            setattr(self._channel, name, value)
