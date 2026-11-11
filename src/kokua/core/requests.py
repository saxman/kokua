"""What every model call in a turn was sent, and why every tool loop ended, from AIMU's run events.

The request is recorded exactly as AIMU sent it (``RequestPrepared.payload``, after every adaptation
AIMU applies), not rebuilt from the conversation afterwards: a rebuilt request would show what Kokua
would send now, could not recover a sub-agent's discarded messages, and would break when history is
truncated or branched. See docs/explanation/architecture.md.

Shaped like ``core/metrics.py``'s ``TurnMetrics`` and fed by the same ``record_event`` seam: a turn
opens one accumulator in ``current_requests``, and every agent run in the turn (entry agent,
sub-agents, critics) reports into it. Calls pair by ``run_id``, because concurrent sub-agents
interleave their events. Within one run a response pairs with that run's most recent open request,
because a run's model calls are sequential: a request left without an answer is older than the one
that is answered, so it stays in the record unanswered rather than shifting every later answer onto
the wrong request. A call is a list entry, not a dict keyed by (run, round): a FallbackClient
records one request per client it tries within a round, and those failed attempts are kept.

Bytes go to content-addressed files under ``payloads/`` (see ``kokua/payloads.py``); the session
holds only the small records. Observation must not break the turn it observes, so a write or a
serialization that fails is logged once per turn and the call is recorded without a reference.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Optional

from aimu.events import ModelTurnFinished, RequestPrepared, RunFinished

from kokua import payloads

logger = logging.getLogger(__name__)

# The running turn's accumulator, set by TurnRunner for the turn's duration, for the reason
# ``core/metrics.py``'s ``current_metrics`` is a ContextVar: concurrent turns on different
# conversations must not record into each other.
current_requests: ContextVar[Optional["TurnRequests"]] = ContextVar("current_requests", default=None)


def _count(payload, key: str) -> Optional[int]:
    """How many entries ``payload[key]`` holds, or None. Every wire provider AIMU ships names its
    conversation ``messages`` and its tool schemas ``tools``; anything else is not counted."""
    value = payload.get(key) if isinstance(payload, dict) else None
    return len(value) if isinstance(value, list) else None


class TurnRequests:
    """One turn's model calls and loop endings; see the module docstring."""

    def __init__(
        self,
        *,
        payloads_path: Path,
        capture: bool,
        notify: Optional[Callable[[str, dict], None]] = None,
        entry_run_id: Optional[str] = None,
    ):
        # The entry agent's own run, named by the turn before it runs. Parentage alone cannot say which
        # run that is: a workflow critic's run has no parent either.
        self.entry_run_id = entry_run_id
        self._payloads_path = payloads_path
        self._capture = capture
        self._notify = notify
        self._calls: list[dict] = []
        self._endings: list[dict] = []
        # Per run, the calls whose request has arrived but whose answer has not, newest last (an answer closes the newest).
        self._open: dict[Optional[str], list[dict]] = {}
        self._write_failure_logged = False
        self._serialization_failure_logged = False

    def __call__(self, event) -> None:
        if isinstance(event, RequestPrepared):
            self._request(event)
        elif isinstance(event, ModelTurnFinished):
            self._finished(event)
        elif isinstance(event, RunFinished):
            self._ending(event)

    def record(self) -> Optional[dict]:
        """This turn's calls and endings, or None when it has neither (so nothing is written)."""
        if not self._calls and not self._endings:
            return None
        return {"entry_run_id": self.entry_run_id, "calls": list(self._calls), "endings": list(self._endings)}

    def _request(self, event: RequestPrepared) -> None:
        text = self._serialized(event.payload)
        call = {
            "run_id": event.run_id,
            "parent_run_id": event.parent_run_id,
            "agent": event.agent,
            "round": event.iteration,
            "provider": event.provider,
            "endpoint": event.endpoint,
            "model": event.model,
            "payload_chars": len(text) if text is not None else None,
            "message_count": _count(event.payload, "messages"),
            "tool_count": _count(event.payload, "tools"),
            "payload_ref": self._saved(text),
            "stop_reason": None,
            "tool_call_count": None,
            "response_ref": None,
            "error": None,
        }
        self._calls.append(call)
        self._open.setdefault(event.run_id, []).append(call)

    def _finished(self, event: ModelTurnFinished) -> None:
        pending = self._open.get(event.run_id)
        if pending:
            call = pending.pop()
        else:
            call = {
                "run_id": event.run_id,
                "parent_run_id": event.parent_run_id,
                "agent": event.agent,
                "round": event.iteration,
                "provider": None,
                "endpoint": None,
                "model": event.model,
                "payload_chars": None,
                "message_count": None,
                "tool_count": None,
                "payload_ref": None,
                "stop_reason": None,
                "tool_call_count": None,
                "response_ref": None,
                "error": None,
            }
            self._calls.append(call)
        call["stop_reason"] = event.stop_reason
        # Recorded whether or not the response file is, since it is how a reader (the web UI's round
        # button) tells a round that asked for tools from one that answered, without fetching anything.
        call["tool_call_count"] = len(event.tool_calls or [])
        call["error"] = type(event.error).__name__ if event.error is not None else None
        if self._capture:
            response = {"text": event.text, "thinking": event.thinking, "tool_calls": list(event.tool_calls or [])}
            call["response_ref"] = self._saved(self._serialized(response))
        self._emit("request", call)

    def _ending(self, event: RunFinished) -> None:
        ending = {
            "run_id": event.run_id,
            "parent_run_id": event.parent_run_id,
            "agent": event.agent,
            "ended_by": event.ended_by,
            "round": event.iteration,
            "max_iterations": event.max_iterations,
            "error": type(event.error).__name__ if event.error is not None else None,
        }
        self._endings.append(ending)
        self._emit("ending", ending)

    def _serialized(self, value) -> Optional[str]:
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            self._log_serialization_failure(str(exc))
            return None

    def _saved(self, text: Optional[str]) -> Optional[str]:
        if not self._capture or text is None:
            return None
        try:
            return payloads.save_text(self._payloads_path, text)
        except (UnicodeEncodeError, OSError) as exc:
            self._log_write_failure(str(exc))
            return None

    def _log_write_failure(self, reason: str) -> None:
        if not self._write_failure_logged:
            self._write_failure_logged = True
            logger.warning("A model request record could not be saved (%s); the turn continues without it.", reason)

    def _log_serialization_failure(self, reason: str) -> None:
        if not self._serialization_failure_logged:
            self._serialization_failure_logged = True
            logger.warning(
                "A model request record could not be serialized (%s); the turn continues without it.", reason
            )

    def _emit(self, kind: str, record: dict) -> None:
        if self._notify is None:
            return
        try:
            self._notify(kind, record)
        except Exception:
            logger.warning("Showing a model request record failed; it is still recorded.", exc_info=True)
