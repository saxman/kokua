"""Model-request failures reach the user with their real cause, not a generic apology."""

from __future__ import annotations

from tests.helpers import MockAsyncModelClient
from kokua.core.assistant import Assistant, ModelConnectionError, ModelRefusalError
from kokua.core.errors import describe_error

from aimu.aio.channels.base import ChannelMessage

from tests.channels import FakeChannel, _config


def _connection_error() -> ModelConnectionError:
    exc = ModelConnectionError("Connection error.")
    exc.__cause__ = OSError("[Errno 61] Connection refused")
    return exc


def _refusal_error() -> ModelRefusalError:
    return ModelRefusalError("The model declined this request.", category="cyber", explanation="Requests exploit code.")


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

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=assistant._active_id)
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

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=assistant._active_id)
    assert channel.sent == ["Sorry, the request failed: ValueError: bad request shape"]


async def test_proactive_surfaces_connection_error_without_raising(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([_connection_error()]))

    await assistant._proactive("remind")  # must not raise; a scheduler crash is the failure mode

    assert len(channel.sent) == 1
    assert "Connection refused" in channel.sent[0]


# ---------------------------------------------------------------------------
# A refusal is not a failure. The model was reached, answered in the time it took, and
# declined; telling the user "the request failed" sends them to look for a broken thing.
# ---------------------------------------------------------------------------


async def test_handle_surfaces_a_refusal_as_a_refusal(tmp_path):
    """Distinct from the generic branch, because the remedy is different: rephrase, or switch model.

    Anthropic returns a refusal as HTTP 200 with no content, so before AIMU raised for it this
    arrived as an empty turn and the loop spent its iterations being refused again.
    """
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([_refusal_error()]))

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=assistant._active_id)

    assert len(channel.sent) == 1
    message = channel.sent[0]
    assert "declined" in message
    assert "the request failed" not in message, "a refusal read as a generic failure"


async def test_a_refusal_carries_the_classifier_category_and_explanation(tmp_path):
    """The category is the only thing that tells a user *which* rephrasing might work."""
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([_refusal_error()]))

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=assistant._active_id)

    assert "cyber" in channel.sent[0]
    assert "Requests exploit code." in channel.sent[0]


async def test_a_refusal_without_a_category_still_reads_as_a_refusal(tmp_path):
    """``category`` and ``explanation`` are both optional; the provider often supplies neither."""
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path), channel, client=MockAsyncModelClient([ModelRefusalError("declined")])
    )

    await assistant._handle(ChannelMessage(text="hi", channel="fake"), conversation_id=assistant._active_id)

    assert "the request failed" not in channel.sent[0], "a refusal read as a generic failure"
    assert "None" not in channel.sent[0], "an absent category leaked into the message"


async def test_proactive_surfaces_a_refusal_without_raising(tmp_path):
    """A scheduled task that gets refused must not take the scheduler down with it (invariant 6)."""
    channel = FakeChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([_refusal_error()]))

    await assistant._proactive("remind")

    assert len(channel.sent) == 1
    assert "declined" in channel.sent[0]
    assert "task failed" not in channel.sent[0], "a refusal read as a generic task failure"
