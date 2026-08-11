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
from tests.helpers import MockAsyncModelClient  # noqa: E402

from kokua.config.schema import AssistantConfig  # noqa: E402
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

    Returns a callable `start(delay=0.0, seed=None) -> base_url`. Servers are torn down after the
    test. Memory, plugins, and sub-agents are off so startup is fast and model-free; the mock client
    handles turns. `seed`, if given, is called with the `AssistantConfig` before the app is built, so
    a test can plant a conversation (e.g. a session with recorded sub-agent events) ahead of startup.
    """
    started: list[tuple] = []

    def start(delay: float = 0.0, seed=None) -> str:
        config = AssistantConfig(memory=False, subagents=False, load_plugins=False, tools=["none"])
        if seed is not None:
            seed(config)
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


def test_sidebar_collapse_resize_persist(page, live_server):
    """The left panel collapses to the icon rail, drag-resizes within its clamp, and both the
    collapsed state and the width survive a reload (localStorage, applied pre-paint)."""
    url = live_server(delay=0.0)
    _open(page, url)
    root = page.locator("html")
    sidebar = page.locator("#sidebar")

    expect(root).not_to_have_attribute("data-sidebar-collapsed", "true")
    expanded_width = sidebar.bounding_box()["width"]
    assert expanded_width > 120

    page.click("#sidebar-toggle")  # collapse to the rail
    expect(root).to_have_attribute("data-sidebar-collapsed", "true")
    expect(page.locator("#conv-list")).to_be_hidden()
    assert sidebar.bounding_box()["width"] < 120

    page.reload()  # collapsed state persists across reloads
    page.wait_for_selector("#conv-list li", state="attached")  # list is display:none while collapsed
    expect(root).to_have_attribute("data-sidebar-collapsed", "true")

    page.click("#sidebar-toggle")  # expand again
    expect(root).not_to_have_attribute("data-sidebar-collapsed", "true")

    # Drag the divider left to shrink the panel, then confirm the new width persists on reload.
    handle = page.locator("#sidebar-resize").bounding_box()
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    page.mouse.down()
    page.mouse.move(handle["x"] - 60, handle["y"] + handle["height"] / 2, steps=5)
    page.mouse.up()
    resized_width = sidebar.bounding_box()["width"]
    assert resized_width < expanded_width

    page.reload()
    page.wait_for_selector("#conv-list li")
    assert abs(sidebar.bounding_box()["width"] - resized_width) < 2


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


def test_subagent_card_replays_with_its_nested_trace(page, live_server):
    """A recorded spawn replays as one foldable card whose nested reasoning, tool call, and generated
    text are the page's own thinking/tool/assistant blocks, each individually foldable. Thinking and
    tool calls start collapsed as they do at the top level; the answer starts open, since it is what
    the reader opened the card for."""
    from aimu.sessions import Session, TinyDBSessionStore

    def seed(config):
        store = TinyDBSessionStore(str(config.sessions_path))
        store.save(
            Session(
                key="seeded",
                messages=[{"role": "user", "content": "research the vendors"}],
                metadata={
                    "title": "seeded",
                    "created_at": "2026-08-10T00:00:00",
                    "updated_at": "2026-08-10T00:00:00",
                    "subagent": {
                        "0": [
                            {"id": "r-1", "role": "researcher", "task": "compare pricing", "status": "running"},
                            {"id": "r-1", "append": {"kind": "reasoning", "text": "fetch each page"}},
                            {"id": "r-1", "append": {"kind": "tool", "name": "get_webpage", "arguments": {"url": "u"}}},
                            {"id": "r-1", "append": {"kind": "answer", "text": "**Vendor A** is cheaper."}},
                            {"id": "r-1", "status": "done"},
                        ]
                    },
                },
            )
        )

    _open(page, live_server(delay=0.0, seed=seed))
    card = page.locator(".bubble.subagent")
    expect(card).to_have_count(1)
    header_label = card.locator("> .fold-header > .fold-label")
    expect(header_label).to_contain_text("researcher")
    expect(header_label).to_contain_text("done")
    card.locator("> .fold-header").click()

    nested = card.locator("> .fold-body > .bubble")
    expect(nested).to_have_count(3)
    thinking, tool, answer = nested.nth(0), nested.nth(1), nested.nth(2)
    expect(thinking).to_have_class(re.compile(r"\bthinking\b"))
    expect(thinking).to_have_class(re.compile(r"\bcollapsed\b"))
    expect(tool).to_have_class(re.compile(r"\btool\b"))
    expect(tool).to_have_class(re.compile(r"\bcollapsed\b"))
    expect(tool.locator(".fold-label")).to_contain_text("get_webpage")
    # The answer block: open, and its markdown rendered as it is for the assistant's own reply.
    expect(answer).to_have_class(re.compile(r"\bassistant\b"))
    expect(answer).not_to_have_class(re.compile(r"\bcollapsed\b"))
    expect(answer.locator(".fold-body")).to_be_visible()
    expect(answer.locator("strong", has_text="Vendor A")).to_have_count(1)
    # Expanding a nested block leaves the others closed: each folds on its own.
    thinking.locator(".fold-header").click()
    expect(thinking).not_to_have_class(re.compile(r"\bcollapsed\b"))
    expect(thinking.locator(".fold-body")).to_contain_text("fetch each page")
    expect(tool).to_have_class(re.compile(r"\bcollapsed\b"))


def test_reviewer_card_still_renders_with_its_own_icon(page, live_server):
    """A planning reviewer's verdict shares the same card frame and persisted map as a spawn's card
    (see `renderSubagent`), which is exactly the regression risk finding C caught: the icon is the
    only at-a-glance way to tell the two apart, and it must stay 🔎 for a reviewer, not 🤖."""
    from aimu.sessions import Session, TinyDBSessionStore

    def seed(config):
        store = TinyDBSessionStore(str(config.sessions_path))
        store.save(
            Session(
                key="seeded",
                messages=[{"role": "user", "content": "plan the launch"}],
                metadata={
                    "title": "seeded",
                    "created_at": "2026-08-10T00:00:00",
                    "updated_at": "2026-08-10T00:00:00",
                    "subagent": {
                        "0": [{"role": "Plan reviewer", "status": "rejected", "issues": ["too vague"], "round": 0}]
                    },
                },
            )
        )

    _open(page, live_server(delay=0.0, seed=seed))
    card = page.locator(".bubble.subagent")
    expect(card).to_have_count(1)
    expect(card.locator(".fold-label")).to_contain_text("🔎")
    expect(card.locator(".fold-label")).to_contain_text("Plan reviewer")
    card.locator(".fold-header").click()
    expect(card.locator("li", has_text="too vague")).to_have_count(1)
