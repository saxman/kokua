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

Nested reasoning and generated text are coalesced when recorded and streamed chunk by chunk when
displayed: the page concatenates consecutive chunks of the same kind into one block either way, so
this keeps the stored JSON proportional to text length rather than to token count. Coalescing looks
only at the turn's own collected list, never at state kept on the reporter itself, so one turn's
coalescing is immune to another turn's interleaved activity even though every conversation's turns
share this one reporter and, by default (``subagents.concurrent``), run concurrently.

A block is closed by anything appended after it, which is what gives a multi-round sub-agent one
answer block per round: a round's tool call or reasoning always sits between two generations. The one
round with nothing in between is the round AIMU drives itself, and that one announces itself. AIMU
yields a ``CONTINUING`` chunk before an injected round, carrying which injection it is (a nudge after
an empty turn, or the forced wrap-up at the round cap) and the prompt it sent, so the card marks that
round the way the parent's own loop marker marks an injected round of its own. An ordinary round needs
no marker at either level, because the tool call between two generations is already that boundary. The
card still keeps no iteration counter of its own: the counter could not have named the injection or
quoted it anyway.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Optional, Union

from aimu.models import StreamChunk, StreamingContentType

from kokua import payloads
from kokua.channels.ui import ChannelUI

logger = logging.getLogger(__name__)

# The running turn's collected sub-agent events, set by TurnRunner for the duration of the turn and
# None outside one. A contextvar copy into a TaskGroup child copies the list *reference*, so
# concurrent spawns append to the one list the turn later persists.
subagent_events: ContextVar[Optional[list[dict]]] = ContextVar("subagent_events", default=None)

# How much of a sub-agent's tool response is kept inline in a recorded card. Anything longer is
# written to a payload file and the card holds this much plus a reference (see ``payloads.py``).
#
# A module constant rather than a config key on purpose. It is not a security control, so
# ``[security]`` is not its home, and it is not a capability an agent declares, so no toolset owns
# it. 4,000 characters is chosen so an ordinary tool result stays inline and unchanged, while the
# case that forced this (a PDF fetched as text, 7.8 MB in one card) spills.
RESPONSE_PREVIEW_CHARS = 4000


class SubagentReporter:
    """Turns one spawn's lifecycle into display frames and recorded events."""

    def __init__(
        self,
        ui: ChannelUI,
        *,
        model_for: Callable[[Optional[str]], Optional[str]],
        thinking_for: Callable[[Optional[str]], Optional[Union[bool, str]]],
        payloads_path: Path,
    ):
        self._ui = ui
        # Resolve a spawn's agent_type to the model it runs on and the reasoning effort it runs at.
        # AIMU's observer callbacks carry neither, but Kokua builds the specs, so it is the side that knows.
        self._model_for = model_for
        self._thinking_for = thinking_for
        # Spawns whose generated text has already been streamed, so finished() knows not to send the
        # accumulated text a second time. Discarded on finish, since this one reporter lives as long
        # as the connection and must not grow an entry per spawn ever made.
        self._streamed_answers: set[str] = set()
        # Where an oversized tool response is spilled to (see ``payloads.py``). A path rather than the
        # whole config, because this is the only setting the reporter reads and taking the config
        # would let it grow a dependency on anything else in there.
        self._payloads_path = payloads_path

    async def spawned(self, spawn_id: str, agent_type: Optional[str], task: str) -> None:
        """Open the card, naming the model this worker runs on and the reasoning effort it runs at, so the
        stored conversation says what produced its answer. ``model`` is omitted when none is configured
        anywhere, since AIMU resolves that case at client construction and there is no string to record;
        ``thinking`` is omitted on ``None`` for the same reason, but recorded on ``False``, which is the
        declaration "do not reason" rather than the absence of one."""
        event = {"id": spawn_id, "role": agent_type or "subagent", "task": task, "status": "running"}
        model = self._model_for(agent_type)
        if model:
            event["model"] = str(model)
        thinking = self._thinking_for(agent_type)
        if thinking is not None:
            event["thinking"] = thinking
        await self._report(event)

    async def chunk(self, spawn_id: str, chunk: StreamChunk) -> None:
        if chunk.phase == StreamingContentType.CONTINUING:
            # The loop injected a prompt of its own. Recorded as an entry rather than inferred from a
            # rise in chunk.iteration, because the counter cannot say which injection it was, and the
            # two say opposite things to the worker: keep working, or stop and answer from what you have.
            call = chunk.content if isinstance(chunk.content, dict) else {}
            await self._report(
                {
                    "id": spawn_id,
                    "append": {"kind": "loop", "reason": call.get("kind"), "text": call.get("prompt", "")},
                }
            )
        elif chunk.phase == StreamingContentType.THINKING:
            if chunk.content:
                await self._report({"id": spawn_id, "append": {"kind": "reasoning", "text": chunk.content}})
        elif chunk.phase == StreamingContentType.TOOL_CALLING:
            call = chunk.content if isinstance(chunk.content, dict) else {}
            await self._report({"id": spawn_id, "append": self._tool_append(call)})
        elif chunk.phase == StreamingContentType.GENERATING:
            if chunk.content:
                self._streamed_answers.add(spawn_id)
                await self._report({"id": spawn_id, "append": {"kind": "answer", "text": chunk.content}})
        # Image/audio progress from a sub-agent is not surfaced.

    def _tool_append(self, call: dict) -> dict:
        """One tool call as a card entry, spilling an oversized response to a payload file.

        Applied here rather than when a card is displayed, so the frame sent live and the entry
        replayed from metadata are the same object: a card that changed shape when the user switched
        away and back would be a worse bug than the size it fixes.
        """
        append = {"kind": "tool", "name": call.get("name"), "arguments": call.get("arguments")}
        response = call.get("response")
        if not isinstance(response, str) or len(response) <= RESPONSE_PREVIEW_CHARS:
            append["response"] = response
            return append
        try:
            reference = payloads.save_text(self._payloads_path, response)
        except (UnicodeEncodeError, OSError) as exc:
            # Two independent ways save_text can fail, both handled the same way. A tool result that
            # reached us already decoded with errors="surrogateescape" (a binary file fetched as text
            # is the likely source) carries lone surrogates that strict UTF-8 cannot encode, so
            # save_text raises UnicodeEncodeError. Separately, writing to disk can fail on its own
            # terms (a full disk, a permissions problem, a payloads directory that cannot be created),
            # raising OSError; before this cap, the TOOL_CALLING branch never touched disk at all, so
            # this callback did not previously depend on a write succeeding. Either way, this callback
            # runs on a live turn's recording path, and an exception here would end the turn rather
            # than merely leave one oversized card, so the response is kept inline, unbounded, exactly
            # as it was before this cap existed.
            logger.warning(
                "A sub-agent tool response for %r could not be written to a payload file (%s); "
                "recording it inline instead.",
                call.get("name"),
                exc,
            )
            append["response"] = response
            return append
        append["response"] = response[:RESPONSE_PREVIEW_CHARS]
        append["response_ref"] = reference
        append["response_bytes"] = len(response)
        return append

    async def finished(self, spawn_id: str, result: str, error: Optional[BaseException]) -> None:
        streamed = spawn_id in self._streamed_answers
        self._streamed_answers.discard(spawn_id)
        event: dict
        if error is not None and not isinstance(error, asyncio.CancelledError):
            event = {"id": spawn_id, "status": "error", "append": {"kind": "error", "text": str(error)}}
        else:
            event = {"id": spawn_id, "status": "done" if error is None else "stopped"}
            # The text is already on screen when it streamed; repeating it here would show the
            # sub-agent's answer twice. Providers that yield no GENERATING chunk stream nothing, so
            # for those the terminal event still carries the text and the card is not left empty.
            if not streamed and result:
                event["append"] = {"kind": "answer", "text": result}
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
        consecutive reasoning or generated-text chunks into one entry.

        Coalescing is decided by looking only at ``collected``'s own last element -- never at state
        kept on the reporter itself. One reporter serves every conversation's turns, and those turns
        run concurrently by default (``subagents.concurrent``), but each turn's ``collected`` is its
        own list (see the ``subagent_events`` contextvar above), so a check scoped to that list keeps
        one turn's coalescing immune to another turn's interleaved activity on the same reporter.
        Within a single turn, an entry is extended only while it remains that list's literal last
        item; anything else appended after it -- another spawn's event, or this spawn's own tool call
        or finish -- closes the block for good, since reopening it would put a later chunk's text
        ahead of whatever was appended in between. That rule is also what gives a multi-round spawn
        one answer entry per round, since a round's tool call breaks the block.
        """
        collected = subagent_events.get()
        if collected is None:
            return
        append = event.get("append")
        kind = append.get("kind") if append is not None else None
        if kind in ("reasoning", "answer"):
            tail = collected[-1] if collected else None
            if tail is not None and tail.get("id") == event["id"] and tail.get("append", {}).get("kind") == kind:
                tail["append"]["text"] += append["text"]
                return
            # A copy, so extending the recorded text can never mutate a frame already sent.
            collected.append({**event, "append": dict(append)})
            return
        collected.append(event)
