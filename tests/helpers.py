"""Test helpers: a mock async model client, and the settings table a config parse needs.

The ``MockAsyncModelClient`` is vendored from AIMU's ``tests/helpers_aio.py`` so Kokua's tests are
self-contained and don't reach into the AIMU repo's test directory.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import MagicMock

from aimu.aio._base import AsyncBaseModelClient
from aimu.models import StreamChunk, StreamingContentType

from kokua.config.table import CORE_RUNTIME_SETTINGS, RuntimeSetting, SettingsTable


def core_table() -> SettingsTable:
    """The table for a test that has to pass one to ``config.file.load`` or ``apply_setting``.

    Core-only, and so *narrower* than a real run's table: the shipped ``planning`` toolset declares its
    own section, which this deliberately omits. Right for a test about a core key -- a config naming a
    toolset's section will read as an unknown key through this table, which is the point when the test is
    not about a contributed setting. A test that needs one uses ``build_settings_table()``, or builds its
    own table with the entries it needs.

    Empty today, since ``CORE_RUNTIME_SETTINGS`` is: nothing in Kokua's own core is runtime-mutable now
    that the display flags are gone. A test that needs a hot key rather than a core-only *schema* wants
    :func:`hot_table` instead.
    """
    return SettingsTable(CORE_RUNTIME_SETTINGS)


# A stand-in hot core setting, for a test that needs one and does not care which. With no shipped core
# runtime settings there is no real key to name, and reaching for the planning toolset's would tie a test
# about the hot-apply path to one capability's current declarations. ``concurrent_tools`` is a real
# ``AssistantConfig`` field, under a fictional ``[core]`` section so this double cannot claim a key the
# shipped schema owns.
HOT_CORE_DOUBLE = RuntimeSetting("concurrent_tools", "core", bool)


def hot_table() -> SettingsTable:
    """``core_table()`` plus one stand-in hot entry, for a test exercising the hot-apply path."""
    return SettingsTable([*CORE_RUNTIME_SETTINGS, HOT_CORE_DOUBLE])


class MockAsyncModelClient(AsyncBaseModelClient):
    """An async ModelClient stub whose chat() responses come from a fixed queue.

    A plain ``str`` entry is a direct response; ``"tool"`` simulates one tool-call round by
    appending the relevant messages and consuming a follow-up response.
    """

    def __init__(self, responses: list):
        self.model = MagicMock()
        self.model.supports_tools = True
        self.model.supports_thinking = False
        self.model.supports_vision = False
        self.model.supports_audio = False
        self.model_kwargs = None
        self._system_message = None
        self.default_generate_kwargs = {}
        self.messages = []
        self.tools = []
        self.last_thinking = ""
        self.last_usage = None
        self.last_output_truncated = False
        self.concurrent_tool_calls = False
        self._responses = list(responses)
        self._call_count = 0

    def _update_generate_kwargs(self, generate_kwargs=None):
        return generate_kwargs or {}

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if stream:
            return self._chat_streamed(user_message, generate_kwargs, use_tools, images=images)
        # Route appends through _append_message (the base mixin's append-with-timestamp seam) so the
        # mock stays faithful to the real clients: every stored message carries the inert `timestamp`.
        if audio:
            from aimu.models._internal.audio_input import _build_audio_content_blocks

            self._append_message({"role": "user", "content": _build_audio_content_blocks(user_message, audio)})
        elif images:
            from aimu.models._internal.image_input import _build_user_content_blocks

            self._append_message({"role": "user", "content": _build_user_content_blocks(user_message, images)})
        else:
            self._append_message({"role": "user", "content": user_message})
        response = self._responses[self._call_count]
        self._call_count += 1

        # An exception in the queue simulates a model-request failure (e.g. an unreachable server).
        if isinstance(response, BaseException):
            raise response

        if response == "tool":
            self._append_message(
                {
                    "role": "assistant",
                    "tool_calls": [{"type": "function", "function": {"name": "mock_tool", "arguments": {}}, "id": "x"}],
                }
            )
            self._append_message({"role": "tool", "name": "mock_tool", "content": "tool result", "tool_call_id": "x"})
            text = self._responses[self._call_count]
            self._call_count += 1
            self._append_message({"role": "assistant", "content": text})
            return text
        self._append_message({"role": "assistant", "content": response})
        return response

    async def _chat_streamed(
        self, user_message, generate_kwargs=None, use_tools=True, images=None
    ) -> AsyncIterator[StreamChunk]:
        response = await self._chat(user_message, generate_kwargs, use_tools, images=images)
        yield StreamChunk(StreamingContentType.GENERATING, response)

    async def _generate(self, prompt, generate_kwargs=None, stream=False, images=None, audio=None):
        if stream:
            return self._generate_streamed(prompt, generate_kwargs)
        return await self._chat(prompt, generate_kwargs, images=images)

    async def _generate_streamed(self, prompt, generate_kwargs=None) -> AsyncIterator[StreamChunk]:
        text = await self._generate(prompt, generate_kwargs)
        yield StreamChunk(StreamingContentType.GENERATING, text)


class BlockingModelClient(MockAsyncModelClient):
    """A client whose streamed reply waits for ``release``, holding a real turn in flight.

    For tests about what else can happen *during* a turn: the turn holds the gate reader and the agent
    lock it really holds, rather than a test standing in for either. Await ``started`` to know the turn
    is in flight, and always ``release.set()`` in a ``finally``, since nothing else ends it.
    """

    def __init__(self, reply: str = "done"):
        super().__init__([reply])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _chat_streamed(self, user_message, generate_kwargs=None, use_tools=True, images=None):
        self.started.set()
        await self.release.wait()
        async for chunk in super()._chat_streamed(user_message, generate_kwargs, use_tools, images=images):
            yield chunk
