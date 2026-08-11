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
length rather than to token count. Coalescing looks only at the turn's own collected list, never at
state kept on the reporter itself, so one turn's coalescing is immune to another turn's interleaved
activity even though every conversation's turns share this one reporter and, by default
(``subagents.concurrent``), run concurrently.
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
        """Append ``event`` to the running turn's own collected list, coalescing a spawn's
        consecutive reasoning chunks into one entry.

        Coalescing is decided by looking only at ``collected``'s own last element -- never at state
        kept on the reporter itself. One reporter serves every conversation's turns, and those turns
        run concurrently by default (``subagents.concurrent``), but each turn's ``collected`` is its
        own list (see the ``subagent_events`` contextvar above), so a check scoped to that list keeps
        one turn's coalescing immune to another turn's interleaved activity on the same reporter.
        Within a single turn, an entry is extended only while it remains that list's literal last
        item; anything else appended after it -- another spawn's event, or this spawn's own tool call
        or finish -- closes the block for good, since reopening it would put a later chunk's text
        ahead of whatever was appended in between.
        """
        collected = subagent_events.get()
        if collected is None:
            return
        append = event.get("append")
        if append is not None and append.get("kind") == "reasoning":
            tail = collected[-1] if collected else None
            if tail is not None and tail.get("id") == event["id"] and tail.get("append", {}).get("kind") == "reasoning":
                tail["append"]["text"] += append["text"]
                return
            # A copy, so extending the recorded text can never mutate a frame already sent.
            collected.append({**event, "append": dict(append)})
            return
        collected.append(event)
