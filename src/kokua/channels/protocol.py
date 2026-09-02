"""The optional rich-frame surface a channel may offer beyond AIMU's base ``Channel``.

This Protocol is documentation and a static-typing target; nothing does ``isinstance`` against it.
The runtime contract lives in :class:`kokua.channels.ui.ChannelUI`, which probes each capability once
and resolves it to a documented fallback, so the core never asks "does this channel have X?".

A new transport implements ``aimu.aio.Channel`` and works. Implementing members of this Protocol
makes it richer: the sidebar, verbose planning traces, and in-page approval prompts all light up as
their frames become available. ``WebChannel`` implements all of it; ``CLIChannel`` implements none.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable

from aimu.models import StreamChunk


@runtime_checkable
class RichChannel(Protocol):
    """A channel that can render more than a stream of text."""

    #: The conversation this transport is currently displaying. The core mirrors the active
    #: conversation onto it so the channel can mute a background turn's frames.
    active_conversation_id: Optional[str]

    async def send_conversations(self, items: list[dict]) -> None:
        """Render the conversation list (a sidebar). Supporting this also enables scheduled tasks to
        run in their own conversation rather than the one being viewed."""

    async def send_history(self, messages: list[dict], metadata: Optional[dict] = None) -> None:
        """Replay one whole conversation, replacing whatever is displayed. Offering this is what lets a
        conversation command typed at the composer repaint the view the core just moved."""

    async def send_working(self, active: bool) -> None:
        """Show or clear a "turn already running" indicator for the conversation now in view."""

    async def send_notification(self, text: str) -> None:
        """Report that a background turn finished, without stealing the current view."""

    async def send_approval_request(self, name: str, arguments: Any) -> None:
        """Prompt for tool-call approval. The reply arrives through the ordinary inbound path."""

    async def send_plan_review_request(self, plan: str, critique: Optional[str] = None) -> None:
        """Prompt for plan approve/edit/reject. The reply arrives through the ordinary inbound path."""

    async def send_plan(self, plan: str) -> None:
        """Render a plan as its own bubble rather than as chat text."""

    async def send_phase(self, label: str, detail: str = "") -> None:
        """Open a labeled phase block. Supporting this is what enables the verbose planning trace."""

    async def send_done(self) -> None:
        """Terminate a streamed sequence that is not followed by a message frame."""

    async def send_subagent(self, event: dict) -> None:
        """Render a sub-agent (reviewer) activity card, updated in place by its ``id``."""

    async def stream_activity(self, chunks: AsyncIterator[StreamChunk], *, show_answer: bool = False) -> str:
        """Stream an agentic loop live without terminating it, returning the generated text."""

    def begin_catch_up(self, conversation_id: str, text: str, image_paths: Optional[list[str]] = None) -> None:
        """Start recording a turn's output, so switching into its conversation mid-turn shows the turn.

        Synchronous, and paired with ``end_catch_up``: both are bookkeeping calls from the core rather
        than frames. Only a channel that can replay a conversation has any use for them."""

    def end_catch_up(self, conversation_id: str) -> None:
        """Stop recording: the turn's output is in the store, or the turn ended without getting there."""
