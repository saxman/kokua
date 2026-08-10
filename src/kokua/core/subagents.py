"""Sub-agent activity, shown in the conversation that spawned it.

AIMU's ``spawn_subagent`` returns only a string, so a delegating turn used to look like a long
pause. :class:`SubagentReporter` implements AIMU's ``SubagentObserver`` and does two independent
jobs per callback: send a ``subagent`` frame (the page renders one foldable card per spawn, updated
in place by ``id``) and append the same event to a per-turn list that the turn persists, so a reload
replays what was seen live.

Display and recording are deliberately separate. A background turn's frames are muted by the
channel, but its events are still recorded, so switching into that conversation shows the work. The
same split is what makes a cancelled spawn recoverable: recording is synchronous, while the send is
best-effort because the reporter is running inside the cancelled task.

Nested reasoning is coalesced when recorded and streamed when displayed: the page concatenates
consecutive reasoning into one block either way, so this keeps the stored JSON proportional to text
length rather than to token count.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Optional

from aimu.models import StreamChunk, StreamingContentType

from kokua.channels.ui import ChannelUI

logger = logging.getLogger(__name__)

# The running turn's collected sub-agent events, set by TurnRunner for the duration of the turn and
# None outside one. A contextvar copy into a TaskGroup child copies the list *reference*, so
# concurrent spawns append to the one list the turn later persists.
subagent_events: ContextVar[Optional[list[dict]]] = ContextVar("subagent_events", default=None)


class SubagentReporter:
    """Turns one spawn's lifecycle into display frames and recorded events."""

    def __init__(self, ui: ChannelUI):
        self._ui = ui
        # The (spawn id, entry) most recently appended to the collected list, but only while that
        # entry is a reasoning block still open for coalescing. Anything else recorded in between --
        # another spawn's event, or this spawn's own tool call or finish -- clears it, so coalescing
        # only ever extends the literal last item in the list rather than reopening an older block.
        self._last_reasoning: Optional[tuple[str, dict]] = None

    async def spawned(self, spawn_id: str, agent_type: Optional[str], task: str) -> None:
        await self._report({"id": spawn_id, "role": agent_type or "subagent", "task": task, "status": "running"})

    async def chunk(self, spawn_id: str, chunk: StreamChunk) -> None:
        if chunk.phase == StreamingContentType.THINKING:
            if chunk.content and self._ui.display_flag("show_thinking", False):
                await self._report({"id": spawn_id, "append": {"kind": "reasoning", "text": chunk.content}})
        elif chunk.phase == StreamingContentType.TOOL_CALLING:
            if self._ui.display_flag("show_tools", False):
                call = chunk.content if isinstance(chunk.content, dict) else {}
                await self._report(
                    {
                        "id": spawn_id,
                        "append": {"kind": "tool", "name": call.get("name"), "arguments": call.get("arguments")},
                    }
                )
        # GENERATING is deliberately dropped: the answer lands once, from finished(), so the card
        # never shows a half-written answer alongside the finished one. Image/audio progress from a
        # sub-agent is not surfaced.

    async def finished(self, spawn_id: str, result: str, error: Optional[BaseException]) -> None:
        if error is None:
            event = {"id": spawn_id, "status": "done", "append": {"kind": "answer", "text": result}}
        elif isinstance(error, asyncio.CancelledError):
            event = {"id": spawn_id, "status": "stopped", "append": {"kind": "answer", "text": result}}
        else:
            event = {"id": spawn_id, "status": "error", "append": {"kind": "error", "text": str(error)}}
        await self._report(event, best_effort=error is not None)

    async def _report(self, event: dict, *, best_effort: bool = False) -> None:
        """Record the event, then show it. Recording first is what makes a cancelled spawn survive:
        the send can fail (or be cancelled outright) once the task it runs in is being torn down."""
        self._record(event)
        if not best_effort:
            await self._ui.show_subagent(event)
            return
        try:
            await self._ui.show_subagent(event)
        except (Exception, asyncio.CancelledError):
            logger.debug("A sub-agent's closing frame could not be sent; it is still recorded.", exc_info=True)

    def _record(self, event: dict) -> None:
        collected = subagent_events.get()
        if collected is None:
            return
        append = event.get("append")
        if append is not None and append.get("kind") == "reasoning":
            if self._last_reasoning is not None and self._last_reasoning[0] == event["id"]:
                self._last_reasoning[1]["append"]["text"] += append["text"]
                return
            # A copy, so extending the recorded text can never mutate a frame already sent.
            entry = {**event, "append": dict(append)}
            self._last_reasoning = (event["id"], entry)
            collected.append(entry)
            return
        self._last_reasoning = None
        collected.append(event)
