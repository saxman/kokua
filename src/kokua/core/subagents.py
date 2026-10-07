"""Sub-agent activity, shown in the conversation that spawned it.

AIMU's ``spawn_subagent`` returns only a string, so a delegating turn used to look like a long
pause. :class:`SubagentReporter` implements AIMU's ``SubagentObserver`` and does two independent
jobs per callback: send a ``subagent`` frame (the page renders one foldable card per spawn, updated
in place by ``id``) and append the same event to a per-turn list that the turn persists, so a reload
replays what was seen live. A third, unrelated to display, rides the same ``spawned``/``finished``
pair: bracketing ``core/messaging.py``'s ``current_address`` around the spawn's run, which is what
keeps a worker's own address from leaking into whoever spawned it once that worker returns. See
``spawned``'s own docstring for why this reporter, rather than anything in ``core/messaging.py``
itself, is where that has to live.

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

AIMU also yields an ``INBOX`` chunk when a message reaches a worker already running, whether the user
sent it or one of the turn's own agents did (``core/messaging.py``), and the card carries it for the
same reason it carries a ``loop`` entry: a round with nothing else between two generations needs its
own marker, or the break reads as unexplained. The two are recorded as distinct kinds rather than one,
because ``loop`` is the agent loop speaking and ``INBOX`` is somebody else's words reaching the
worker; a card that filed both under ``loop`` would credit the loop with what a person, or another
agent, said.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Callable, Optional, Union

from aimu.models import StreamChunk, StreamingContentType

from kokua import payloads
from kokua.channels.ui import ChannelUI
from kokua.core.messaging import current_address

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
        # The `current_address` (core/messaging.py) token saved per spawn, keyed by spawn_id the same
        # way `_streamed_answers` is: a UUID AIMU mints once per spawn, so concurrent spawns sharing
        # this one reporter never collide on the key even though neither dict is otherwise isolated
        # per turn. See `spawned`/`finished` for what this buys. The same lifetime concern
        # `_streamed_answers` above carries applies here too, and the route to it is through AIMU
        # rather than only through a hand-built call: `spawned` is awaited *outside* `_run_observed`'s
        # own `try`/`finally`, and the notifier around it catches `Exception` alone, so a
        # `BaseException` raised inside `spawned` escapes with `finished` never called. The one that
        # happens is a `CancelledError` landing on the card send inside `spawned` (a `/stop`, or a
        # shutdown reaching the websocket), which is after that method's first statement has already
        # taken the token and put it here. That leaks one entry, and worse than
        # `_streamed_answers`'s leak, leaves `current_address` cleared to `None` in whatever Context
        # that spawn ran in, rather than merely growing this dict.
        # Fail-closed in both halves, which is why it is recorded rather than guarded: `None` is the
        # value `send_message` refuses on, the next drain in that Context sets it again, and the
        # Context in question belongs to a turn that is being cancelled.
        self._address_tokens: dict[str, Token[Optional[str]]] = {}
        # Where an oversized tool response is spilled to (see ``payloads.py``). A path rather than the
        # whole config, because this is the only setting the reporter reads and taking the config
        # would let it grow a dependency on anything else in there.
        self._payloads_path = payloads_path

    async def spawned(self, spawn_id: str, agent_type: Optional[str], task: str) -> None:
        """Open the card, naming the model this worker runs on and the reasoning effort it runs at, so the
        stored conversation says what produced its answer. ``model`` is omitted when none is configured
        anywhere, since AIMU resolves that case at client construction and there is no string to record;
        ``thinking`` is omitted on ``None`` for the same reason, but recorded on ``False``, which is the
        declaration "do not reason" rather than the absence of one.

        Also clears ``current_address`` (core/messaging.py) for the run about to start, saving a token
        ``finished`` restores. This fires before AIMU calls ``agent.run`` at all (``spawned`` is the
        first thing ``_run_observed`` does), and it is the one hook every spawn Kokua makes shares,
        declared or composed: both ``core/agents.py`` and ``toolsets/capabilities.py`` build their
        ``spawn_subagent`` with this reporter as ``observer``. That matters because `current_address`
        is not Task-scoped -- a round with one tool call (the common case) dispatches it with a plain
        ``await``, not ``TaskGroup.create_task``, so the spawned run shares this run's own ``Context``
        for as long as it runs and for however long afterward nothing else touches the var. Clearing
        here rather than only saving forces the question "did this run claim an address of its own":
        one that opens a reader overwrites ``None`` with it the moment ``run()`` calls ``_open_inbox``,
        before its first model call; one that does not (``toolsets/capabilities.py``'s composed worker
        was the one shipped case, fixed alongside this; any future path making the same mistake is the
        reason this exists rather than a one-off patch there) leaves it ``None`` for its own tool
        calls, which is what lets ``send_message`` refuse a forgetful worker instead of mis-attributing
        it to whoever spawned it. Restoring on ``finished`` is the other half: without it, the spawning
        run's *own* later tool calls in the same round (dispatched sequentially, sharing the same
        Context) would still see the child's leftover address once the child returns.
        """
        self._address_tokens[spawn_id] = current_address.set(None)
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
        elif chunk.phase == StreamingContentType.INBOX:
            # A message reached this worker while it was running -- the user's, or one of the
            # turn's own agents' (`core/messaging.py`'s `send_message`). Recorded as its own kind
            # rather than as a `loop` entry, because the card's reader needs to see that somebody
            # said this, not the loop.
            #
            # The text carries its own attribution and nothing here adds a second one. An agent's
            # words arrive already prefixed `[message from {sender}]` by `core/messaging.py`'s
            # `_for_model`, which is the drain, not this card: that prefix is what the *model*
            # reading the next round sees, so it has to be in the text itself rather than a sibling
            # field, and it is the one load-bearing copy. A CLI's own phase marker (AIMU's `[message]`
            # line) or this card's own `message` label names the phase beside it, which is
            # commentary, not a second attribution; a card or a terminal line that also stamped
            # the sender would be repeating the same fact in two places; a field here would be
            # the other way principle 1 forbids (addressing pushed into the channel).
            sent = chunk.content if isinstance(chunk.content, dict) else {}
            await self._report({"id": spawn_id, "append": {"kind": "message", "text": sent.get("text", "")}})
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
        # Restores `current_address` to whatever `spawned` saved before clearing it, undoing that
        # clear regardless of how this spawn ended (`_run_observed` calls this from a `finally`, so a
        # cancelled or failed spawn restores exactly like a successful one). Popped rather than left,
        # for the same reason `_streamed_answers` is discarded below: this reporter outlives any one
        # spawn. `reset` is safe here because `finished` always runs in the same Context `spawned` set
        # the token in -- both calls happen inside the one coroutine `_run_observed` awaits, never
        # across a `TaskGroup` boundary.
        address_token = self._address_tokens.pop(spawn_id, None)
        if address_token is not None:
            current_address.reset(address_token)
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
