"""Files a tool made for the user, as opposed to text it returned to the model.

A tool result has two audiences. The model reads the text the tool returns; a person wants the file
(an image, for instance). Publishing is the second half, done on its own: the tool hands the file's
reference to the running turn's sink, which records it for replay and shows it on the channel.

The sink is per turn and reached through a ContextVar, for the reason ``subagent_events`` is (see
``core/subagents.py``): turns on different conversations run concurrently, and a contextvar copy into
a spawned worker's task copies the reference, so a worker's image reaches the turn that spawned it
with no plumbing through the spawn. That is why a worker's image shows live: the worker's own
stream never reaches the page, but its context does.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from kokua.channels.ui import ChannelUI

logger = logging.getLogger(__name__)


class ArtifactSink:
    """One turn's published artifacts, and the channel they are shown on.

    ``position`` answers where in the conversation's message list the publishing call sits: the
    index of the newest assistant message, the one whose tool call is running. Replay places the
    artifact after that message's tool items. A position that cannot be found is recorded as ``None``,
    which replay places at the turn's end.
    """

    def __init__(self, ui: "ChannelUI", position: Callable[[], Optional[int]]):
        self._ui = ui
        self._position = position
        self.items: list[dict] = []

    async def publish(self, url: str, *, filename: str, mime: str, path: Path) -> None:
        try:
            after = self._position()
        except Exception:
            # The file is saved whether or not its place is known, so a failed lookup must not become a
            # tool error. No anchor replays at the turn's end, which is where an unplaced file belongs.
            logger.warning("An artifact's position was not found; it replays at the turn's end.", exc_info=True)
            after = None
        record = {"url": url, "filename": filename, "mime": mime, "after": after}
        # Recorded before it is shown, and a failed show is swallowed: the file exists either way, and
        # a closed socket must not turn a finished image into a tool error the model reports as failure.
        self.items.append(record)
        try:
            await self._ui.show_artifact(record, path)
        except Exception:
            # A closed socket is absorbed inside the channel's own send, so reaching here is a channel bug.
            logger.warning("An artifact could not be shown; it is still recorded.", exc_info=True)


# The running turn's sink, set by TurnRunner for the turn's duration and None outside one.
current_artifacts: ContextVar[Optional[ArtifactSink]] = ContextVar("current_artifacts", default=None)


async def publish(url: str, *, filename: str, mime: str, path: Path) -> bool:
    """Publish a file to the running turn, returning whether a turn was there to take it."""
    sink = current_artifacts.get()
    if sink is None:
        return False
    await sink.publish(url, filename=filename, mime=mime, path=path)
    return True
