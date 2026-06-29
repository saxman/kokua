"""Test helpers: a mock async model client.

Vendored from AIMU's ``tests/helpers_aio.py`` (the ``MockAsyncModelClient``) so Kokua's tests are
self-contained and don't reach into the AIMU repo's test directory.
"""

from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import MagicMock

from aimu.aio._base import AsyncBaseModelClient
from aimu.models import StreamChunk, StreamingContentType


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
        self.concurrent_tool_calls = False
        self._responses = list(responses)
        self._call_count = 0

    def _update_generate_kwargs(self, generate_kwargs=None):
        return generate_kwargs or {}

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if stream:
            return self._chat_streamed(user_message, generate_kwargs, use_tools, images=images)
        if audio:
            from aimu.models._internal.audio_input import _build_audio_content_blocks

            self.messages.append({"role": "user", "content": _build_audio_content_blocks(user_message, audio)})
        elif images:
            from aimu.models._internal.image_input import _build_user_content_blocks

            self.messages.append({"role": "user", "content": _build_user_content_blocks(user_message, images)})
        else:
            self.messages.append({"role": "user", "content": user_message})
        response = self._responses[self._call_count]
        self._call_count += 1

        if response == "tool":
            self.messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [{"type": "function", "function": {"name": "mock_tool", "arguments": {}}, "id": "x"}],
                }
            )
            self.messages.append({"role": "tool", "name": "mock_tool", "content": "tool result", "tool_call_id": "x"})
            text = self._responses[self._call_count]
            self._call_count += 1
            self.messages.append({"role": "assistant", "content": text})
            return text
        self.messages.append({"role": "assistant", "content": response})
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
