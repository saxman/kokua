"""Shared fakes: MCP servers and model clients that behave in a specific, scripted way.

Here rather than in one test module because several packages' tests need them, and a test importing
another test module makes the suite's layout load-bearing.
"""

from __future__ import annotations

import asyncio


from tests.helpers import MockAsyncModelClient


def _fake_mcp_tool(name: str):
    async def fn(**kwargs):
        return "ok"

    fn.__name__ = name
    fn.__tool_spec__ = {"function": {"name": name}}
    fn.__tool_is_async__ = True
    fn.__tool_is_streaming__ = False
    return fn


class _FakeMCP:
    def __init__(self, tools):
        self._tools = tools
        self.closed = False

    async def as_tools(self):
        return self._tools

    async def aclose(self):
        self.closed = True


async def _await_value(value):
    return value


class _SeedsSystemMessage:
    """Mixin: seed the system message into an empty transcript, the way the real clients do.

    AIMU's ``_append_user_turn`` prepends the system message when ``messages`` is empty, so a
    conversation's *first* turn produces ``[system, user, assistant, ...]`` and its user message sits
    one past the pre-run length. ``MockAsyncModelClient`` overrides ``_chat`` and so never seeds one,
    which is why the suite could not see a first-turn off-by-one in the index a turn's sub-agent cards
    are filed under. Mix in ahead of the client to get the real transcript:
    ``class C(_SeedsSystemMessage, MockAsyncModelClient)``.
    """

    def __init__(self, *args, system_message: str = "You are a test assistant.", **kwargs):
        super().__init__(*args, **kwargs)
        self._system_message = system_message  # after super(), which sets it to None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if len(self.messages) == 0 and self._system_message:
            self._append_message({"role": "system", "content": self._system_message})
        return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)


class _BlockingStreamClient(MockAsyncModelClient):
    """Records the user turn, signals it started, then hangs until the turn task is cancelled."""

    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        self.messages.append({"role": "user", "content": user_message})
        self.started.set()
        await asyncio.Event().wait()  # hang until cancelled


class _RequestsToolOnce(MockAsyncModelClient):
    """Requests one gated tool call on the first turn (single-turn: the Agent's engine dispatches it),
    then answers plainly. Lets a real run exercise the approval gate instead of the mock's faked round."""

    def __init__(self, name: str, arguments: dict):
        super().__init__([])
        self._name = name
        self._arguments = arguments
        self._requested = False

    async def _chat(
        self, user_message=None, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None
    ):
        if user_message is not None:
            self.messages.append({"role": "user", "content": user_message})
        if not self._requested:
            self._requested = True
            self.messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"type": "function", "function": {"name": self._name, "arguments": self._arguments}, "id": "x"}
                    ],
                }
            )
            return ""
        self.messages.append({"role": "assistant", "content": "ok"})
        return "ok"
