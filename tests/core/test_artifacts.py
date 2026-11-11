"""The per-turn artifact sink: records first, shows second, and is inert outside a turn."""

from __future__ import annotations

import asyncio
from pathlib import Path

from kokua.core.artifacts import ArtifactSink, current_artifacts, publish


class _UI:
    def __init__(self, fail: bool = False):
        self.shown: list[tuple[dict, Path]] = []
        self._fail = fail

    async def show_artifact(self, record: dict, path: Path) -> None:
        if self._fail:
            raise ConnectionError("socket closed")
        self.shown.append((record, path))


def _run(coro):
    return asyncio.run(coro)


def test_publish_outside_a_turn_does_nothing():
    assert _run(publish("/images/a.png", filename="a.png", mime="image/png", path=Path("/x/a.png"))) is False


def test_publish_records_with_its_anchor_and_shows():
    ui = _UI()
    sink = ArtifactSink(ui, position=lambda: 4)

    async def go():
        token = current_artifacts.set(sink)
        try:
            return await publish("/images/a.png", filename="cat.png", mime="image/png", path=Path("/x/a.png"))
        finally:
            current_artifacts.reset(token)

    assert _run(go()) is True
    record = {"url": "/images/a.png", "filename": "cat.png", "mime": "image/png", "after": 4}
    assert sink.items == [record]
    assert ui.shown == [(record, Path("/x/a.png"))]


def test_a_failed_send_still_records_and_does_not_raise():
    sink = ArtifactSink(_UI(fail=True), position=lambda: 2)

    async def go():
        token = current_artifacts.set(sink)
        try:
            return await publish("/images/a.png", filename="a.png", mime="image/png", path=Path("/x/a.png"))
        finally:
            current_artifacts.reset(token)

    assert _run(go()) is True
    assert len(sink.items) == 1


def test_a_failed_position_lookup_records_no_anchor_and_does_not_raise():
    ui = _UI()

    def position():
        raise RuntimeError("conversation dropped")

    sink = ArtifactSink(ui, position=position)

    _run(sink.publish("/images/a.png", filename="cat.png", mime="image/png", path=Path("/x/a.png")))

    record = {"url": "/images/a.png", "filename": "cat.png", "mime": "image/png", "after": None}
    assert sink.items == [record]
    assert ui.shown == [(record, Path("/x/a.png"))]
