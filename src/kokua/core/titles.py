"""A conversation title written by the model, from the message that opened the conversation.

The fallback this replaces is still there and still runs first: ``messages.derive_title`` truncates
the first user message to 40 characters, which is what the sidebar shows the moment a conversation
gets one. This module is the upgrade that arrives a second later, and everything here is shaped by
that ordering. The call is best-effort by construction: a failure, a refusal, or an endpoint that is
simply not there returns None and the placeholder stands, because a conversation whose title is a
little blunt is not a conversation the user should be told about.

The client is fresh and context-free, the same choice ``workflows/critics.py`` makes and for a
related reason: this is not the conversation talking to itself. It holds one system prompt and one
user message, so nothing the assistant said can steer the title, and no tool can run.
"""

from __future__ import annotations

import logging
from typing import Optional

from aimu import aio

from kokua.core.messages import TITLE_MAX

logger = logging.getLogger(__name__)

# How much of the first message the model is shown. A title needs the opening, not the whole of a
# pasted log, and this is the one place a first message's length turns into tokens spent.
MAX_INPUT = 2000

TITLE_SYSTEM_MESSAGE = (
    "You write short titles for conversations, the way a chat sidebar labels them. "
    "Answer with the title alone: at most six words, no quotation marks, no trailing period, "
    "no preamble, and no explanation."
)

TITLE_PROMPT_PREFIX = "Write the title for a conversation that opens with this message:\n\n"


def _sanitize(answer: str) -> Optional[str]:
    """The usable title in a model's answer, or None.

    Written against what small local models actually return rather than what the prompt asked for:
    a preamble line ("Here is a title:"), the title in quotes, a trailing period, or several lines
    where one was requested. The *last* non-empty line is the title in every one of those shapes,
    which is why this reads from the end.
    """
    lines = [line.strip() for line in answer.splitlines()]
    candidate = next((line for line in reversed(lines) if line), "")
    # One strip set rather than a sequence of them, so the order the model happened to nest its
    # decorations in ("Kauai trip". vs "Kauai trip.") cannot leave half of one behind.
    candidate = " ".join(candidate.strip(" \t\"'`*.:").split())
    if not candidate:
        return None
    if len(candidate) > TITLE_MAX:
        cut = candidate[:TITLE_MAX]
        # A word boundary if there is one to cut on, so the sidebar does not show half a word. Only
        # when it leaves most of the title: a first "word" longer than the bound has no boundary to
        # find, and a hard cut is better than a title reduced to nothing.
        space = cut.rfind(" ")
        candidate = cut[:space].rstrip() if space > TITLE_MAX // 2 else cut.rstrip()
    return candidate or None


async def summarize_title(model: str, first_message: str) -> Optional[str]:
    """Ask *model* for a title for a conversation opening with *first_message*, or None.

    None covers every way this can fail to produce something worth showing, because the caller's
    response to all of them is the same: keep the placeholder. That includes a client that will not
    build (a model string naming nothing reachable) and an endpoint that is down, both of which are
    ordinary on a workstation where the model server is not always running.

    ``CancelledError`` is deliberately not caught. Shutdown cancels this call rather than waiting on
    it, so the cancellation has to reach the task that is being torn down.
    """
    try:
        client = aio.client(model, system=TITLE_SYSTEM_MESSAGE)
        answer = await client.chat(
            TITLE_PROMPT_PREFIX + first_message[:MAX_INPUT],
            use_tools=False,
            # A title is not worth a reasoning trace, and on a local server it is the difference
            # between one second and thirty. Silent on a model with no reasoning to disable.
            thinking=False,
        )
    except Exception:
        logger.warning("Could not generate a conversation title; keeping the derived one", exc_info=True)
        return None
    return _sanitize(answer or "")
