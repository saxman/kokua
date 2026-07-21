"""Model-request failures reach the user with their real cause, not a generic apology."""

from __future__ import annotations

from helpers import MockAsyncModelClient
from kokua.assistant import Assistant, ModelConnectionError
from kokua.errors import describe_error

from aimu.aio.channels.base import ChannelMessage

from test_assistant import FakeChannel, _config


def _connection_error() -> ModelConnectionError:
    exc = ModelConnectionError("Connection error.")
    exc.__cause__ = OSError("[Errno 61] Connection refused")
    return exc


def test_describe_error_includes_root_cause():
    exc = ModelConnectionError("Connection error.")
    exc.__cause__ = OSError("[Errno 61] Connection refused")
    text = describe_error(exc)
    assert "ModelConnectionError: Connection error." in text
    assert "OSError: [Errno 61] Connection refused" in text


def test_describe_error_single_link():
    assert describe_error(RuntimeError("boom")) == "RuntimeError: boom"


def test_describe_error_truncates():
    text = describe_error(RuntimeError("x" * 500), max_length=40)
    assert len(text) == 40
    assert text.endswith("…")


async def test_handle_surfaces_connection_error(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([_connection_error()]))

    await assistant._handle(ChannelMessage(text="hi", channel="fake"))

    assert len(channel.sent) == 1
    message = channel.sent[0]
    assert "model server" in message
    assert "Connection refused" in message
    assert "something went wrong" not in message


async def test_handle_surfaces_generic_error_detail(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client=MockAsyncModelClient([ValueError("bad request shape")])
    )

    await assistant._handle(ChannelMessage(text="hi", channel="fake"))

    assert channel.sent == ["Sorry, the request failed: ValueError: bad request shape"]


async def test_proactive_surfaces_connection_error_without_raising(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([_connection_error()]))

    await assistant._proactive("remind")  # must not raise; a scheduler crash is the failure mode

    assert len(channel.sent) == 1
    assert "Connection refused" in channel.sent[0]
