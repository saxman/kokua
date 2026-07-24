"""Browser-driven end-to-end tests of the web UI's client JS (opt-in: ``pytest -m e2e``).

These cover the one surface pytest otherwise can't reach: the page script in ``web_static/index.html``
turning server frames into DOM. The server-side frame contract (what frames are emitted, and the
muting/gating that decides them) is already unit-tested in ``test_web.py`` against a fake socket; here
we run the real page in headless Chromium against a live server so the client's rendering is exercised
too -- notification banners, the "working" indicator, and that a background turn's output never leaks
into the conversation being viewed.

Deselected by default (``addopts = -m 'not e2e'``); run with ``uv run pytest -m e2e``. Needs the ``web``
extra and a Chromium (``uv run playwright install chromium``). The tests are skipped (not errored) when
those aren't installed, so the default mock-only suite stays green without them.
"""

from __future__ import annotations

import asyncio
import re
import socket
import threading
import time

import pytest

# Opt-in suite: skip cleanly at collection when the browser/server deps aren't installed, so the
# default `-m 'not e2e'` run never errors on a machine without the web extra or Playwright.
uvicorn = pytest.importorskip("uvicorn")
sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

from aimu.models import StreamChunk, StreamingContentType  # noqa: E402
from helpers import MockAsyncModelClient  # noqa: E402

from kokua.config import AssistantConfig  # noqa: E402
from kokua.frontends.web import build_app  # noqa: E402

pytestmark = pytest.mark.e2e

REPLY = "Hello from the assistant."
_HIDDEN = re.compile(r"(^|\s)hidden(\s|$)")


class _SlowClient(MockAsyncModelClient):
    """A mock model client that streams its reply immediately, then holds the turn open for `delay`.

    Streaming the reply token up front makes the turn *observably* live and bound to the conversation
    it started in (the token renders there), so a test can wait for it before switching away -- which
    both eliminates the send-then-switch bind race and lets it then drive background muting, the
    completion notification, and the switch-in "working" indicator deterministically. The turn's
    `done` frame is what's delayed, so it is still in flight while the test switches conversations."""

    def __init__(self, delay: float = 0.0, reply: str = REPLY):
        super().__init__([])
        self._delay = delay
        self._reply = reply

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if stream:
            return self._chat_streamed(user_message, generate_kwargs, use_tools, images=images)
        await asyncio.sleep(self._delay)
        self.messages.append({"role": "user", "content": user_message})
        self.messages.append({"role": "assistant", "content": self._reply})
        return self._reply

    async def _chat_streamed(self, user_message, generate_kwargs=None, use_tools=True, images=None):
        self.messages.append({"role": "user", "content": user_message})
        yield StreamChunk(StreamingContentType.GENERATING, self._reply)  # renders now in the viewed conversation
        await asyncio.sleep(self._delay)  # hold the turn open (its `done` is delayed) so a test can switch away
        self.messages.append({"role": "assistant", "content": self._reply})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server():
    """Factory: start the real web app (backed by a `_SlowClient`) under uvicorn in a thread.

    Returns a callable `start(delay=0.0) -> base_url`. Servers are torn down after the test. Memory,
    plugins, and sub-agents are off so startup is fast and model-free; the mock client handles turns.
    """
    started: list[tuple] = []

    def start(delay: float = 0.0) -> str:
        config = AssistantConfig(memory=False, subagents=False, load_plugins=False, tools=["none"])
        app = build_app(config, client_factory=lambda conversation_id: _SlowClient(delay))
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn server did not start in time"
        started.append((server, thread))
        return f"http://127.0.0.1:{port}"

    yield start

    for server, thread in started:
        server.should_exit = True
        thread.join(timeout=10)


def _open(page, url: str) -> None:
    """Load the page and wait until the WebSocket is up (the sidebar list has rendered)."""
    page.goto(url)
    page.wait_for_selector("#conv-list li")


def test_send_message_renders_reply(page, live_server):
    """Core frame->DOM path: a sent message renders a user bubble and the streamed reply."""
    _open(page, live_server(delay=0.0))
    page.fill("#msg", "ping")
    page.click("#send")
    expect(page.locator(".bubble.user", has_text="ping")).to_be_visible()
    expect(page.locator(".bubble", has_text=REPLY)).to_be_visible(timeout=10_000)


def test_bubbles_show_timestamp_caption(page, live_server):
    """The user bubble and the streamed reply each carry a datetime caption (`.bubble-ts`)."""
    _open(page, live_server(delay=0.0))
    page.fill("#msg", "ping")
    page.click("#send")
    # User bubble stamped at submit; assistant bubble stamped when the stream finalizes.
    expect(page.locator(".bubble.user .bubble-ts")).to_be_visible()
    expect(page.locator(".bubble.assistant .bubble-ts")).to_be_visible(timeout=10_000)


def test_background_turn_notifies_and_does_not_leak(page, live_server):
    """Switching away mid-turn: the reply is muted (never rendered in the now-viewed conversation) and
    the turn's completion surfaces as a dismissible notification banner instead."""
    _open(page, live_server(delay=2.0))
    page.fill("#msg", "ping")
    page.click("#send")
    # Wait until the turn is observably running in this conversation (its token rendered here), so the
    # switch below can't race the turn's binding -- it is already bound to this conversation.
    expect(page.locator(".bubble.assistant", has_text=REPLY)).to_be_visible(timeout=10_000)

    page.click("#new-conv")  # switch away while the turn's `done` is still pending -> it finishes muted
    expect(page.locator("#conv-list li")).to_have_count(2)
    expect(page.locator(".bubble.assistant")).to_have_count(0)  # the fresh conversation shows no reply

    banner = page.locator("#notifications .notification-banner")
    expect(banner).to_be_visible(timeout=15_000)  # the background turn's completion surfaces here instead
    expect(page.locator(".bubble.assistant", has_text=REPLY)).to_have_count(0)  # reply never leaked into view

    banner.locator("button").click()  # dismiss
    expect(page.locator("#notifications .notification-banner")).to_have_count(0)


def test_working_indicator_on_switch_into_running(page, live_server):
    """Switching back into a conversation whose turn is still running shows the 'working' indicator,
    which clears once that turn completes."""
    _open(page, live_server(delay=3.0))
    page.fill("#msg", "ping")
    page.click("#send")
    # Confirm the turn is running here (token rendered) before switching, so it is bound to this
    # conversation and genuinely still in flight when we switch back into it below.
    expect(page.locator(".bubble.assistant", has_text=REPLY)).to_be_visible(timeout=10_000)

    page.click("#new-conv")  # switch away; sidebar becomes [new, original]
    expect(page.locator("#conv-list li")).to_have_count(2)

    working = page.locator("#working-indicator")
    expect(working).to_have_class(_HIDDEN)  # the fresh conversation is idle
    page.locator("#conv-list li").nth(1).click()  # back into the original, still-running conversation
    expect(working).to_be_visible()  # .hidden is display:none, so visible == indicator shown
    expect(working).not_to_have_class(_HIDDEN)

    expect(working).to_have_class(_HIDDEN, timeout=10_000)  # clears once the turn completes
