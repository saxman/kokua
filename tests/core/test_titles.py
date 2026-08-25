"""Generated conversation titles: the model call, and what it will accept as a title."""

from __future__ import annotations

import asyncio

import pytest

from kokua.core import titles

# Bound here, at import, so the suite-wide ``no_generated_titles`` stub (which replaces the module
# attribute) does not stand in for the function this module is about.
from kokua.core.titles import summarize_title
from tests.helpers import MockAsyncModelClient


class _RecordingClient(MockAsyncModelClient):
    """A mock client that keeps the ``use_tools`` its chat call was made with."""

    def __init__(self, reply: str):
        super().__init__([reply])
        self.use_tools = None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        self.use_tools = use_tools
        return await super()._chat(
            user_message, generate_kwargs, use_tools=use_tools, stream=stream, images=images, audio=audio
        )


def _patch_client(monkeypatch, client, seen: list = None):
    def factory(model=None, system=None, **kwargs):
        if seen is not None:
            seen.append((model, system))
        return client

    monkeypatch.setattr(titles.aio, "client", factory)


async def test_a_generated_title_is_the_models_answer(monkeypatch):
    _patch_client(monkeypatch, MockAsyncModelClient(["Kauai trip planning"]))
    assert await summarize_title("provider:model", "help me plan a trip to Kauai") == "Kauai trip planning"


async def test_a_quoted_title_loses_its_quotes(monkeypatch):
    _patch_client(monkeypatch, MockAsyncModelClient(['"Kauai trip planning".']))
    assert await summarize_title("provider:model", "plan a trip") == "Kauai trip planning"


async def test_a_preamble_line_is_dropped_in_favour_of_the_title(monkeypatch):
    _patch_client(monkeypatch, MockAsyncModelClient(["Here is a title:\nKauai trip planning"]))
    assert await summarize_title("provider:model", "plan a trip") == "Kauai trip planning"


async def test_an_overlong_title_is_truncated_at_a_word_boundary(monkeypatch):
    _patch_client(monkeypatch, MockAsyncModelClient(["Planning a two week family holiday on the island of Kauai"]))
    title = await summarize_title("provider:model", "plan a trip")
    assert title == "Planning a two week family holiday on"
    assert len(title) <= titles.TITLE_MAX


async def test_an_empty_answer_yields_no_title(monkeypatch):
    _patch_client(monkeypatch, MockAsyncModelClient(["   \n  "]))
    assert await summarize_title("provider:model", "plan a trip") is None


async def test_an_endpoint_failure_yields_no_title_rather_than_raising(monkeypatch):
    _patch_client(monkeypatch, MockAsyncModelClient([ConnectionError("no route to host")]))
    assert await summarize_title("provider:model", "plan a trip") is None


async def test_a_client_that_will_not_build_yields_no_title(monkeypatch):
    def boom(model=None, system=None, **kwargs):
        raise ValueError("unknown model")

    monkeypatch.setattr(titles.aio, "client", boom)
    assert await summarize_title("provider:model", "plan a trip") is None


async def test_the_title_call_is_context_free_and_toolless(monkeypatch):
    client = _RecordingClient("Kauai trip planning")
    seen: list = []
    _patch_client(monkeypatch, client, seen)

    await summarize_title("provider:model@http://host:1234", "help me plan a trip to Kauai")

    assert seen == [("provider:model@http://host:1234", titles.TITLE_SYSTEM_MESSAGE)]
    assert client.use_tools is False
    assert [m["role"] for m in client.messages] == ["user", "assistant"]
    assert "help me plan a trip to Kauai" in client.messages[0]["content"]


async def test_a_long_first_message_is_truncated_before_it_is_sent(monkeypatch):
    client = _RecordingClient("A long story")
    _patch_client(monkeypatch, client)

    await summarize_title("provider:model", "x" * (titles.MAX_INPUT + 500))

    assert len(client.messages[0]["content"]) <= titles.MAX_INPUT + len(titles.TITLE_PROMPT_PREFIX)


async def test_cancellation_is_not_swallowed(monkeypatch):
    class _Hanging(MockAsyncModelClient):
        async def _chat(self, *args, **kwargs):
            raise asyncio.CancelledError()

    _patch_client(monkeypatch, _Hanging(["never"]))
    with pytest.raises(asyncio.CancelledError):
        await summarize_title("provider:model", "plan a trip")
