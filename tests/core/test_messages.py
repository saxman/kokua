"""Transcript helpers: reading text out of a message, and deriving a conversation title.

The image-rewriting half of this module is covered by tests/test_images.py.
"""

from __future__ import annotations

from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_KEY, PROVENANCE_PROACTIVE

from kokua.core.messages import derive_title, first_user_text, is_user_turn, message_text


def test_message_text_reads_a_plain_string():
    assert message_text("hello") == "hello"


def test_message_text_joins_the_text_blocks_of_a_multimodal_message():
    content = [
        {"type": "text", "text": "look at "},
        {"type": "image_url", "image_url": {"url": "/images/a.png"}},
        {"type": "text", "text": "this"},
    ]
    assert message_text(content) == "look at this"


def test_message_text_of_an_image_only_message_is_empty():
    content = [{"type": "image_url", "image_url": {"url": "/images/a.png"}}]
    assert message_text(content) == ""


def test_message_text_tolerates_absent_or_odd_content():
    assert message_text(None) == ""
    assert message_text(123) == ""


def test_derive_title_uses_the_first_user_message():
    messages = [
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "plan my week"},
        {"role": "user", "content": "and my month"},
    ]
    assert derive_title(messages) == "plan my week"


def test_derive_title_is_none_without_a_user_message():
    assert derive_title([{"role": "assistant", "content": "hi"}]) is None
    assert derive_title([]) is None


def test_derive_title_is_none_for_an_image_only_first_turn():
    """An image with no caption has no words to title the conversation with."""
    content = [{"type": "image_url", "image_url": {"url": "/images/a.png"}}]
    assert derive_title([{"role": "user", "content": content}]) is None


def test_derive_title_is_bounded_and_single_line():
    long_first_turn = "word " * 200
    title = derive_title([{"role": "user", "content": long_first_turn + "\nsecond line"}])
    assert title and len(title) < len(long_first_turn)
    assert "\n" not in title


def test_first_user_text_is_the_whole_message_the_title_truncates():
    """The generated title is written from the message, not from the 40-character placeholder."""
    long_message = "plan my week " * 10
    messages = [{"role": "user", "content": long_message}]
    assert first_user_text(messages) == long_message.strip()
    assert derive_title(messages) == first_user_text(messages)[:40]


def test_first_user_text_is_none_without_a_user_message():
    assert first_user_text([{"role": "assistant", "content": "hi"}]) is None


def test_is_user_turn_accepts_a_message_the_user_sent():
    assert is_user_turn({"role": "user", "content": "hello"})


def test_is_user_turn_rejects_a_loop_injected_user_message():
    injected = {"role": "user", "content": "continue", PROVENANCE_KEY: PROVENANCE_CONTINUATION}
    assert not is_user_turn(injected)


def test_is_user_turn_rejects_other_roles():
    assert not is_user_turn({"role": "assistant", "content": "hi"})
    assert not is_user_turn({"role": "tool", "content": "12:00"})


def test_is_user_turn_accepts_a_user_message_with_unrelated_provenance():
    # PROVENANCE_PROACTIVE tags an unattended run's own messages, not a loop injection.
    assert is_user_turn({"role": "user", "content": "brief me", PROVENANCE_KEY: PROVENANCE_PROACTIVE})
