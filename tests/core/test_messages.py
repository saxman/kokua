"""Transcript helpers: reading text out of a message, and deriving a conversation title.

The image-rewriting half of this module is covered by tests/test_images.py.
"""

from __future__ import annotations

from kokua.core.messages import derive_title, message_text


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
