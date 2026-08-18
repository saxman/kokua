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
from tests.channels import example_agents  # noqa: E402
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
    completion notification, and the switch-in "working" indicator deterministically.

    `tail`, if given, is streamed as a second chunk *after* the delay, i.e. after the test has switched
    conversations. That is what makes a test of muting mean anything: with the reply alone, the only
    frame still in flight across the switch is the turn's `done` terminator, which renders no content,
    so a leak of the reply's remaining tokens would go unnoticed.

    `tool_response`, if given, streams one TOOL_CALLING chunk carrying it ahead of the reply. AIMU yields
    that phase only once a call has returned, so the chunk carries the call and its result together --
    which is what a live tool card renders from."""

    def __init__(self, delay: float = 0.0, reply: str = REPLY, tail: str = "", tool_response: str = ""):
        super().__init__([])
        self._delay = delay
        self._reply = reply
        self._tail = tail
        self._tool_response = tool_response

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if stream:
            return self._chat_streamed(user_message, generate_kwargs, use_tools, images=images)
        await asyncio.sleep(self._delay)
        self.messages.append({"role": "user", "content": user_message})
        self.messages.append({"role": "assistant", "content": self._reply + self._tail})
        return self._reply + self._tail

    async def _chat_streamed(self, user_message, generate_kwargs=None, use_tools=True, images=None):
        self.messages.append({"role": "user", "content": user_message})
        if self._tool_response:
            yield StreamChunk(
                StreamingContentType.TOOL_CALLING,
                {"name": "get_webpage", "arguments": {"url": "u"}, "response": self._tool_response},
            )
        yield StreamChunk(StreamingContentType.GENERATING, self._reply)  # renders now in the viewed conversation
        await asyncio.sleep(self._delay)  # hold the turn open so a test can switch away mid-reply
        if self._tail:
            yield StreamChunk(StreamingContentType.GENERATING, self._tail)  # arrives after that switch
        self.messages.append({"role": "assistant", "content": self._reply + self._tail})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server():
    """Factory: start the real web app (backed by a `_SlowClient`) under uvicorn in a thread.

    Returns a callable `start(delay=0.0, seed=None, tail="") -> base_url`. Servers are torn down after the
    test. Plugins are off so startup is fast and model-free; the mock client handles turns. The agents
    are the shipped ones (`Assistant.create` refuses an empty set), and nothing here spawns a
    sub-agent, so they only need to exist. `seed`, if given, is called with the `AssistantConfig`
    before the app is built, so a test can plant a conversation (e.g. a session with recorded
    sub-agent events) ahead of startup.
    """
    started: list[tuple] = []

    def start(delay: float = 0.0, seed=None, tail: str = "", tool_response: str = "") -> str:
        config = AssistantConfig(agents=example_agents(), entry_agent="assistant", load_plugins=False)
        if seed is not None:
            seed(config)
        app = build_app(
            config,
            client_factory=lambda conversation_id: _SlowClient(delay, tail=tail, tool_response=tool_response),
        )
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


def test_send_and_stop_swap_places(page, live_server):
    """One primary action, never a disabled button: Send is replaced by Stop for the duration of a
    turn. Switching conversations mid-turn must hand Send back, or the composer of the conversation you
    switched into would be unusable."""
    _open(page, live_server(delay=3.0))
    send, stop = page.locator("#send"), page.locator("#stop")
    expect(send).to_be_visible()
    expect(stop).to_be_hidden()

    page.fill("#msg", "ping")
    send.click()
    expect(stop).to_be_visible()
    expect(send).to_be_hidden()

    page.click("#new-conv")  # switching away leaves that turn running in the background
    expect(send).to_be_visible()  # ...and this conversation is idle, so it can be typed into
    expect(stop).to_be_hidden()

    page.locator("#conv-list li").nth(1).click()  # back into the still-running conversation
    expect(stop).to_be_visible()  # the composer follows the conversation you are viewing
    expect(stop).to_be_hidden(timeout=10_000)  # ...and hands Send back when that turn ends


def test_enter_sends_and_shift_enter_makes_a_newline(page, live_server):
    """A textarea, not an input: a message can contain a newline. Enter still sends, because a chat
    composer that needs a button click for every message is worse than the multi-line it enables."""
    _open(page, live_server(delay=0.0))
    box = page.locator("#msg")
    box.click()
    box.type("first")
    page.keyboard.press("Shift+Enter")
    box.type("second")
    assert box.input_value() == "first\nsecond"

    page.keyboard.press("Enter")
    expect(page.locator(".bubble.user", has_text="first")).to_be_visible()
    assert box.input_value() == ""
    # Exactly one: the keydown handler calls requestSubmit(), and a second listener or a surviving
    # native submit would echo the message twice.
    expect(page.locator(".bubble.user")).to_have_count(1)


def test_bubbles_show_timestamp_caption(page, live_server):
    """The user bubble and the streamed reply each carry a datetime caption (`.bubble-ts`)."""
    _open(page, live_server(delay=0.0))
    page.fill("#msg", "ping")
    page.click("#send")
    # User bubble stamped at submit; assistant bubble stamped when the stream finalizes.
    expect(page.locator(".bubble.user .bubble-ts")).to_be_visible()
    expect(page.locator(".bubble.assistant .bubble-ts")).to_be_visible(timeout=10_000)


def test_transcript_is_one_flat_column_with_hover_timestamps(page, live_server):
    """The flat transcript: user and assistant rows share a left edge instead of alternating sides,
    and the datetime caption is revealed on hover rather than printed under every block. The caption
    stays in the DOM at zero opacity, which is why the older visibility assertions still hold."""
    _open(page, live_server(delay=0.0))
    page.fill("#msg", "ping")
    page.click("#send")
    user = page.locator(".bubble.user")
    assistant = page.locator(".bubble.assistant")
    expect(assistant).to_be_visible(timeout=10_000)

    # Same left edge: the bubble metaphor put these on opposite sides of the pane.
    assert abs(user.bounding_box()["x"] - assistant.bounding_box()["x"]) < 2

    # The user's turn is marked by a glyph, not by a fill.
    expect(user.locator(".row-marker")).to_have_text(">")

    caption = user.locator(".bubble-ts")
    assert caption.evaluate("el => getComputedStyle(el).opacity") == "0"
    user.hover()
    expect(caption).not_to_have_css("opacity", "0")


TAIL = "And here is the rest of it."


def test_background_turn_notifies_and_does_not_leak(page, live_server):
    """Switching away mid-turn: the rest of the reply is muted (never rendered in the now-viewed
    conversation) and the turn's completion surfaces as a dismissible notification banner instead.

    `tail` is the part of the reply that streams *after* the switch, which is the part a once-per-send
    mute decision would have leaked into the conversation the user moved to."""
    _open(page, live_server(delay=2.0, tail=TAIL))
    page.fill("#msg", "ping")
    page.click("#send")
    # Wait until the turn is observably running in this conversation (its token rendered here), so the
    # switch below can't race the turn's binding -- it is already bound to this conversation.
    expect(page.locator(".bubble.assistant", has_text=REPLY)).to_be_visible(timeout=10_000)

    page.click("#new-conv")  # switch away mid-reply -> the rest of the turn finishes muted
    expect(page.locator("#conv-list li")).to_have_count(2)
    expect(page.locator(".bubble.assistant")).to_have_count(0)  # the fresh conversation shows no reply

    banner = page.locator("#notifications .notification-banner")
    expect(banner).to_be_visible(timeout=15_000)  # the background turn's completion surfaces here instead
    expect(page.locator(".bubble.assistant")).to_have_count(0)  # nothing of the turn leaked into view
    assert TAIL not in page.locator("#log").inner_text()

    banner.locator("button").click()  # dismiss
    expect(page.locator("#notifications .notification-banner")).to_have_count(0)


def test_switching_back_mid_turn_shows_the_turn_so_far_and_keeps_streaming(page, live_server):
    """Switching back into a conversation whose turn is still running: the `history` frame it gets can
    only carry the store, which has none of this turn yet, so the turn's own output rides along as
    catch-up items -- the user bubble and the answer streamed so far -- and the rest streams into that
    same bubble."""
    url = live_server(delay=2.0, tail=TAIL)
    _open(page, url)
    page.fill("#msg", "ping")
    page.click("#send")
    expect(page.locator(".bubble.assistant", has_text=REPLY)).to_be_visible(timeout=10_000)

    page.click("#new-conv")  # away, mid-reply; sidebar becomes [new, original]
    expect(page.locator(".bubble.assistant")).to_have_count(0)
    page.locator("#conv-list li").nth(1).click()  # ...and back, while the turn is still in flight

    # Replayed from the catch-up record, not from the store: neither is persisted yet.
    expect(page.locator(".bubble.user", has_text="ping")).to_be_visible()
    expect(page.locator(".bubble.assistant", has_text=REPLY)).to_be_visible()
    expect(page.locator("#working-indicator")).not_to_have_class(_HIDDEN)  # still marked as running

    # The tail then streams into the bubble the replay left open: one bubble holding the whole reply.
    expect(page.locator(".bubble.assistant", has_text=TAIL)).to_be_visible(timeout=15_000)
    expect(page.locator(".bubble.assistant")).to_have_count(1)
    assert page.locator("#notifications .notification-banner").count() == 0  # never backgrounded at the end


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


def test_sidebar_row_shows_the_conversation_age(page, live_server):
    """The `conversations` frame already carries `updated_at` and the page used to discard it. A second
    dim line per row answers 'which of these did I touch recently' without opening any."""
    _open(page, live_server(delay=0.0, seed=_seed_tool_call("the page body")))
    row = page.locator("#conv-list li").first
    expect(row.locator(".conv-title")).to_have_text("seeded")
    # Seeded at a fixed 2026-08-10 date, so this is a calendar date rather than a relative age.
    expect(row.locator(".conv-age")).to_have_text("Aug 10")


# --- Settings panel -------------------------------------------------------------------------------


def test_settings_panel_planning_checkbox_round_trips(page, live_server):
    """A toolset-contributed setting's wire key is namespaced (`planning.plan_review`), not the bare
    checkbox id (`set-plan_review`): this is the one automated guard on that wire format, since
    `test_web.py` only exercises the server side against a fake socket and can't catch app.js reading
    or writing the wrong key. Toggling the checkbox, saving, and reloading would silently do nothing
    (the checkbox would render unchecked again) if app.js still spoke the flat `plan_review` key the
    core settings (`show_thinking`, `show_tools`) keep."""
    captured: list = []
    url = live_server(delay=0.0, seed=captured.append)
    _open(page, url)

    page.click("#settings-btn")
    checkbox = page.locator("#set-plan_review")
    expect(checkbox).to_be_visible()
    expect(checkbox).not_to_be_checked()

    checkbox.check()
    page.locator("#settings-form button[type=submit]").click()
    expect(page.locator("#settings-modal")).to_be_hidden()

    # Reload and re-open: a fresh `get_settings` round trip must reflect what was saved.
    page.reload()
    page.wait_for_selector("#conv-list li")
    page.click("#settings-btn")
    expect(page.locator("#set-plan_review")).to_be_checked()

    written = captured[0].config_path.read_text(encoding="utf-8")
    assert "[planning]" in written
    assert "plan_review = true" in written


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
    """A recorded spawn replays as one foldable card shaped like the `spawn_subagent` tool block it
    stands in for: the tool's name and the role on the collapsed header, its arguments on the first
    body line. Below those, the nested reasoning, tool call, and generated text are the page's own
    thinking/tool/assistant blocks, each individually foldable. Thinking and tool calls start
    collapsed as they do at the top level; the answer starts open, since it is what the reader
    opened the card for."""
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
    expect(card).to_have_class(re.compile(r"\bspawn\b"))
    header_label = card.locator("> .fold-header > .fold-label")
    expect(header_label).to_contain_text("spawn_subagent(researcher)")
    expect(header_label).to_contain_text("done")
    # The task rides the argument line, not the header, so it is not truncated.
    expect(header_label).not_to_contain_text("compare pricing")
    card.locator("> .fold-header").click()

    args = card.locator("> .fold-body > .sa-args")
    expect(args).to_have_text('agent_type="researcher", task="compare pricing"')

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


def _seed_tool_call(result: str | None):
    """Return a `seed` planting one conversation whose turn called a tool, with `result` as the tool's
    reply (or no result message at all when it is None)."""
    from aimu.sessions import Session, TinyDBSessionStore

    messages: list[dict] = [
        {"role": "user", "content": "look it up"},
        {
            "role": "assistant",
            "content": "done",
            "tool_calls": [
                {"type": "function", "function": {"name": "get_webpage", "arguments": {"url": "u"}}, "id": "1"}
            ],
        },
    ]
    if result is not None:
        messages.append({"role": "tool", "name": "get_webpage", "content": result, "tool_call_id": "1"})

    def seed(config):
        TinyDBSessionStore(str(config.sessions_path)).save(
            Session(
                key="seeded",
                messages=messages,
                metadata={
                    "title": "seeded",
                    "created_at": "2026-08-10T00:00:00",
                    "updated_at": "2026-08-10T00:00:00",
                },
            )
        )

    return seed


def test_tool_card_shows_what_the_call_returned(page, live_server):
    """The output rides a nested foldable of its own, so the arguments stay scannable when the card is
    opened and the result is one more click away rather than a wall of text."""
    _open(page, live_server(delay=0.0, seed=_seed_tool_call("the page body")))
    tool = page.locator(".bubble.tool")
    expect(tool).to_have_count(1)
    output = tool.locator(".bubble.tool-output")
    expect(output).to_have_count(1)
    expect(output).to_have_class(re.compile(r"\bcollapsed\b"))
    # The size is on the collapsed header, so a reader knows what opening it costs.
    expect(output.locator(".fold-label")).to_contain_text("13 chars")

    tool.locator("> .fold-header").click()
    expect(tool.locator("> .fold-body")).to_contain_text('url="u"')
    output.locator("> .fold-header").click()
    expect(output.locator(".output-text")).to_have_text("the page body")


def test_a_large_tool_output_is_clamped_until_asked_for(page, live_server):
    """Output travels whole but renders clamped: a multi-megabyte result must not become a
    multi-megabyte DOM node just because someone opened its card."""
    _open(page, live_server(delay=0.0, seed=_seed_tool_call("x" * 4500)))
    tool = page.locator(".bubble.tool")
    output = tool.locator(".bubble.tool-output")
    expect(output.locator(".fold-label")).to_contain_text("4,500 chars")

    tool.locator("> .fold-header").click()
    output.locator("> .fold-header").click()
    assert len(output.locator(".output-text").inner_text()) == 4000

    output.locator("button.output-more").click()
    assert len(output.locator(".output-text").inner_text()) == 4500
    expect(output.locator("button.output-more")).to_have_count(0)


def test_a_live_tool_card_shows_the_output_as_it_arrives(page, live_server):
    """The streamed path, not just replay: a `tool` frame carries the result with the call, so the card
    is complete the moment it appears and needs no later update."""
    _open(page, live_server(delay=0.0, tool_response="the page body"))
    page.fill("#msg", "look it up")
    page.click("#send")
    tool = page.locator(".bubble.tool")
    expect(tool).to_have_count(1, timeout=10_000)
    output = tool.locator(".bubble.tool-output")
    expect(output.locator(".fold-label")).to_contain_text("13 chars")
    tool.locator("> .fold-header").click()
    output.locator("> .fold-header").click()
    expect(output.locator(".output-text")).to_have_text("the page body")


def test_a_call_with_no_recorded_result_renders_no_output_section(page, live_server):
    """A conversation stored before results were replayed has calls but no results, and must still
    render the card it always did rather than an empty output row."""
    _open(page, live_server(delay=0.0, seed=_seed_tool_call(None)))
    tool = page.locator(".bubble.tool")
    expect(tool).to_have_count(1)
    expect(tool.locator(".bubble.tool-output")).to_have_count(0)


def _seed_thinking_and_continuation(config):
    """Plant a conversation holding a reasoning block and a framework-injected continuation turn, the
    two rows whose whole visible line is their kind word. Turns `show_thinking` on, since replay gates
    reasoning on the same flag the live stream does."""
    from aimu.sessions import Session, TinyDBSessionStore

    config.show_thinking = True
    config.show_tools = True
    TinyDBSessionStore(str(config.sessions_path)).save(
        Session(
            key="seeded",
            messages=[
                {"role": "user", "content": "find the pricing page"},
                {
                    "role": "assistant",
                    "content": "Looking.",
                    "thinking": "Search first.\nThen read it.",
                    "tool_calls": [
                        {"type": "function", "function": {"name": "get_webpage", "arguments": {"url": "u"}}, "id": "1"}
                    ],
                },
                {"role": "tool", "name": "get_webpage", "content": "body", "tool_call_id": "1"},
                {"role": "user", "content": "Continue working on the task.", "provenance": "continuation"},
                {"role": "assistant", "content": "Done."},
            ],
            metadata={"title": "seeded", "created_at": "2026-08-10T00:00:00", "updated_at": "2026-08-10T00:00:00"},
        )
    )


def test_a_row_whose_kind_word_is_its_only_content_stays_legible(page, live_server):
    """A thinking or continuation row carries no call, so its kind word is the entire visible line and
    has to be the row's primary content. Styled as the dimmer secondary label it wears beside a tool
    call's arguments, such a row reads as blank space and the reasoning looks like it never happened."""
    _open(page, live_server(delay=0.0, seed=_seed_thinking_and_continuation))
    thinking = page.locator(".bubble.thinking")
    loop = page.locator(".bubble.loop")
    expect(thinking).to_have_count(1)
    expect(loop).to_have_count(1)

    # The colour a machine row's primary content uses, taken from the one row that has a payload.
    primary = page.locator(".bubble.tool > .fold-header .fold-payload").evaluate("el => getComputedStyle(el).color")
    for row in (thinking, loop):
        kind = row.locator(".fold-kind")
        assert kind.evaluate("el => getComputedStyle(el).color") == primary
        # Not shrunk relative to its own row either: 0.82em of an already-small row is a whisper.
        ratio = kind.evaluate(
            "el => parseFloat(getComputedStyle(el).fontSize) / parseFloat(getComputedStyle(el.closest('.fold-header')).fontSize)"
        )
        assert ratio == 1, f"kind word is {ratio:.2f}x its row's font size"


def test_a_collapsed_tool_line_names_the_call_and_its_result_size(page, live_server):
    """Everything in the transcript stays folded, so the one always-visible line has to carry the
    call. The arguments ride the header (ellipsized, never wrapped) and the result size sits at the
    right, so a reader learns what was called and what it cost without opening anything."""
    _open(page, live_server(delay=0.0, seed=_seed_tool_call("the page body")))
    tool = page.locator(".bubble.tool")
    expect(tool).to_have_class(re.compile(r"\bcollapsed\b"))

    label = tool.locator("> .fold-header > .fold-label")
    expect(label.locator(".fold-kind")).to_have_text("tool")
    expect(label.locator(".fold-payload")).to_have_text('get_webpage(url="u")')
    expect(label.locator(".fold-metric")).to_have_text("13 chars")

    # One line tall regardless of argument length: the payload ellipsizes, the row never wraps.
    header = tool.locator("> .fold-header")
    line_height = header.evaluate("el => parseFloat(getComputedStyle(el).lineHeight)")
    assert header.bounding_box()["height"] < line_height * 1.8


def test_reviewer_card_is_distinguishable_from_a_spawn(page, live_server):
    """A planning reviewer's verdict shares the same card frame and persisted map as a spawn's card
    (see `renderSubagent`), and the kind word is the only at-a-glance way to tell the two apart: a
    reviewer reads "review" and must not take the tool-call look a spawn gets, since no tool call
    backs a reviewer's verdict."""
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
    expect(card).not_to_have_class(re.compile(r"\bspawn\b"))
    expect(card.locator(".fold-kind")).to_have_text("review")
    expect(card.locator(".fold-label")).to_contain_text("Plan reviewer")
    expect(card.locator(".sa-args")).to_have_count(0)
    card.locator(".fold-header").click()
    expect(card.locator("li", has_text="too vague")).to_have_count(1)


def test_a_card_built_from_an_append_alone_still_names_itself(page, live_server):
    """An `append` frame carries neither role nor status, so a card first created by one has no identity
    to put on its header. It must still label itself: a headerless card is an invisible failure, which is
    how the lost create event behind it (a switch-in mid-task, before `_run_unattended` recorded its
    turn) showed up in the first place -- as a block of sub-agent output with nothing above it."""
    from aimu.sessions import Session, TinyDBSessionStore

    def seed(config):
        TinyDBSessionStore(str(config.sessions_path)).save(
            Session(
                key="seeded",
                messages=[{"role": "user", "content": "research the vendors"}],
                metadata={
                    "title": "seeded",
                    "created_at": "2026-08-10T00:00:00",
                    "updated_at": "2026-08-10T00:00:00",
                    "subagent": {"0": [{"id": "r-1", "append": {"kind": "answer", "text": "Vendor A is cheaper."}}]},
                },
            )
        )

    _open(page, live_server(delay=0.0, seed=seed))
    card = page.locator(".bubble.subagent")
    expect(card).to_have_count(1)
    expect(card.locator("> .fold-header > .fold-label")).to_contain_text("Sub-agent")
    expect(card.locator("> .fold-body")).to_contain_text("Vendor A is cheaper.")


# --- Scheduled tasks sidebar section -------------------------------------------------------------


def _seed_task_and_conversations(config, *, task_id="t1", enabled=True, with_task=True):
    """Plant one interval task plus two conversations it minted and one ordinary chat."""
    from aimu.sessions import Session, TinyDBSessionStore

    from kokua import scheduling

    if with_task:
        scheduling.add(
            config.scheduled_tasks_path,
            {
                "id": task_id,
                "name": "morning-brief",
                "prompt": "Summarize my calendar and unread mail.",
                "schedule": {"type": "daily", "at": "07:00"},
                "max_conversations": 0,
                "created_at": "2026-08-01T00:00:00",
                "enabled": enabled,
            },
        )
    store = TinyDBSessionStore(str(config.sessions_path))
    store.save(
        Session(
            key="chat",
            messages=[{"role": "user", "content": "my own chat"}],
            metadata={"title": "my own chat", "created_at": "2026-08-12T00:00:00", "updated_at": "2026-08-12T00:00:00"},
        )
    )
    for index in (1, 2):
        store.save(
            Session(
                key=f"firing-{index}",
                messages=[{"role": "user", "content": "Summarize my calendar and unread mail."}],
                metadata={
                    "title": "morning-brief",
                    "created_at": f"2026-08-1{index}T07:00:00",
                    "updated_at": f"2026-08-1{index}T07:00:00",
                    "task_id": task_id,
                },
            )
        )


def test_tasks_section_is_absent_with_no_tasks(page, live_server):
    """The section costs nothing for a user who never schedules a task."""
    _open(page, live_server(delay=0.0))
    expect(page.locator("#tasks")).to_be_hidden()


def test_tasks_section_lists_a_task_with_its_schedule_and_countdown(page, live_server):
    _open(page, live_server(delay=0.0, seed=_seed_task_and_conversations))
    row = page.locator(".task-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("morning-brief")
    expect(row.locator(".task-when")).to_contain_text("daily 07:00")
    expect(row.locator(".task-when")).to_contain_text("in ")  # a countdown, not raw seconds
    expect(page.locator("#tasks-count")).to_have_text("1")


def test_task_conversations_nest_under_the_task_and_leave_the_chat_list(page, live_server):
    """The whole point of the grouping: a task's firings are reachable under it, and the chat list
    holds only conversations the user started."""
    _open(page, live_server(delay=0.0, seed=_seed_task_and_conversations))
    expect(page.locator("#task-list .task-conv")).to_have_count(2)
    # The chat list keeps the user's own conversation plus the empty one the app opens at startup,
    # and neither firing appears in it.
    titles = page.locator("#conv-list .conv-title").all_inner_texts()
    assert "my own chat" in titles
    assert "morning-brief" not in titles


def test_a_task_conversation_can_be_opened_from_its_task(page, live_server):
    _open(page, live_server(delay=0.0, seed=_seed_task_and_conversations))
    page.locator("#task-list .task-conv").first.click()
    expect(page.locator("#conv-heading")).to_have_text("morning-brief")
    expect(page.locator(".bubble.user")).to_contain_text("Summarize my calendar and unread mail.")


def test_a_task_conversation_can_be_deleted_from_its_task(page, live_server):
    """A task that mints a conversation per firing piles them up under its row, so each one needs the
    delete its chat-list twin has always had. Reloaded at the end: the row must be gone because the
    server deleted the conversation, not because the click removed a node."""
    page.on("dialog", lambda dialog: dialog.accept())
    _open(page, live_server(delay=0.0, seed=_seed_task_and_conversations))
    rows = page.locator("#task-list .task-conv")
    expect(rows).to_have_count(2)
    surviving = rows.nth(1).locator(".task-conv-label").inner_text()

    rows.first.hover()
    rows.first.locator(".task-conv-delete").click()

    expect(rows).to_have_count(1)
    expect(rows.locator(".task-conv-label")).to_have_text(surviving)

    page.reload()
    page.wait_for_selector("#conv-list li")
    expect(page.locator("#task-list .task-conv")).to_have_count(1)


def test_deleting_a_task_conversation_does_not_also_open_it(page, live_server):
    """The row is click-to-switch, so the delete button has to stop the event reaching it: otherwise
    deleting a run first navigates into the run being deleted. Asserted on which row stays active
    rather than on the heading, because every firing of a task carries the same title and so a switch
    between two of them would not move it."""
    page.on("dialog", lambda dialog: dialog.accept())
    _open(page, live_server(delay=0.0, seed=_seed_task_and_conversations))
    rows = page.locator("#task-list .task-conv")
    expect(rows).to_have_count(2)
    active_label = page.locator("#task-list .task-conv.active .task-conv-label").inner_text()

    rows.nth(1).hover()  # the row that is NOT the conversation being viewed
    rows.nth(1).locator(".task-conv-delete").click()

    expect(rows).to_have_count(1)
    expect(page.locator("#task-list .task-conv.active .task-conv-label")).to_have_text(active_label)


def test_an_orphaned_task_conversation_falls_back_to_the_chat_list(page, live_server):
    """A conversation whose task is gone must stay reachable. Excluding every conversation carrying a
    task_id would hide it from the sidebar entirely."""
    _open(page, live_server(delay=0.0, seed=lambda c: _seed_task_and_conversations(c, with_task=False)))
    expect(page.locator("#tasks")).to_be_hidden()
    titles = page.locator("#conv-list .conv-title").all_inner_texts()
    assert titles.count("morning-brief") == 2


def test_disabling_a_task_round_trips_through_the_server(page, live_server):
    _open(page, live_server(delay=0.0, seed=_seed_task_and_conversations))
    row = page.locator(".task-row")
    expect(row).to_have_class(re.compile(r"(^|\s)enabled(\s|$)"))

    row.hover()
    row.locator(".task-actions button").first.click()  # the pause button

    expect(page.locator(".task-row")).not_to_have_class(re.compile(r"(^|\s)enabled(\s|$)"))
    expect(page.locator(".task-row .task-when")).to_contain_text("disabled")


def test_enabling_a_disabled_task_round_trips_through_the_server(page, live_server):
    _open(page, live_server(delay=0.0, seed=lambda c: _seed_task_and_conversations(c, enabled=False)))
    row = page.locator(".task-row")
    expect(row).not_to_have_class(re.compile(r"(^|\s)enabled(\s|$)"))

    row.hover()
    row.locator(".task-actions button").first.click()  # the play button

    expect(page.locator(".task-row")).to_have_class(re.compile(r"(^|\s)enabled(\s|$)"))
    expect(page.locator(".task-row .task-when")).to_contain_text("in ")


def test_tasks_section_collapses_and_remembers_it(page, live_server):
    url = live_server(delay=0.0, seed=_seed_task_and_conversations)
    _open(page, url)
    expect(page.locator("#task-list")).to_be_visible()

    page.click("#tasks-toggle")
    expect(page.locator("#task-list")).to_be_hidden()

    page.reload()
    page.wait_for_selector("#conv-list li")
    expect(page.locator("#tasks")).to_be_visible()
    expect(page.locator("#task-list")).to_be_hidden()  # the choice survived the reload
