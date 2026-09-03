"""Transcript helpers: message text/title derivation and image-block url rewriting.

Pure functions with no `Assistant` coupling, split out of the assistant core. The image helpers
bridge AIMU's inline base64 message content and Kokua's on-disk `/images/<name>` store (see
`images.py`): compact on persist, expand before `agent.restore`.
"""

from __future__ import annotations

from typing import Optional

from aimu.models import PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER, PROVENANCE_KEY

from kokua import images


# How much title a sidebar has room for: the placeholder is truncated to it and a generated title
# (``core/titles.py``, which imports it) is bounded by it, so the two cannot disagree.
TITLE_MAX = 40

# The user-role messages the agent loop injects between tool-calling iterations. They are not
# something a user sent, so a transcript leaves them out and a turn does not end at one.
INJECTED_USER_PROVENANCE = frozenset({PROVENANCE_CONTINUATION, PROVENANCE_FINAL_ANSWER})


def is_user_turn(message: dict) -> bool:
    """Whether *message* is one the user actually sent, rather than a nudge the loop injected.

    Lives here rather than in either caller because two of them need the same answer for opposite
    reasons: a transcript drops an injected turn from what was said, and a branch must not treat one
    as the end of the turn it is copying, which would cut a turn off in the middle of its own tool
    loop and lose the answer being branched from.
    """
    return message.get("role") == "user" and message.get(PROVENANCE_KEY) not in INJECTED_USER_PROVENANCE


def message_text(content) -> str:
    """Plain text of a message's content (a string, or the text blocks of a multimodal list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def first_user_text(messages: list[dict]) -> Optional[str]:
    """The text of the message that opened the conversation, or None if it had none.

    Split out from :func:`derive_title` rather than inlined there because the two callers want
    different amounts of it: the placeholder is a truncation of this, while the generated title
    (``core/titles.py``) is written from the whole message.
    """
    for message in messages:
        if message.get("role") == "user":
            text = message_text(message.get("content")).strip()
            if text:
                return text
    return None


def derive_title(messages: list[dict]) -> Optional[str]:
    """A conversation title from the first user message (stripped, truncated), or None.

    The immediate placeholder, shown while ``core/titles.py`` writes the real one, and the fallback
    that stands if that call fails.
    """
    text = first_user_text(messages)
    return text[:TITLE_MAX] if text else None


def resolve_user_index(messages: list[dict], base_len: int) -> int:
    """Where a turn's user message sits in ``messages``, or -1 if the turn committed none.

    ``base_len`` is the transcript length before the turn ran, so the turn's own user message is the
    first ``user`` entry at or after it. Resolved by scanning rather than assumed to be ``base_len``
    itself, because AIMU appends the system message as part of a conversation's *first* turn (see
    ``_append_user_turn``), which puts that turn's user message one position later than the pre-run
    length. The index is the key ``core.transcripts.replay_items`` replays a turn's sub-agent cards and
    verbose trace under, so an index that misses by one lands on another message and the replay is
    silently dropped.

    Every caller shares this so the reactive, planned, and unattended paths cannot drift apart; -1 is
    the sentinel the recording no-op guards already understand.
    """
    for index in range(max(base_len, 0), len(messages)):
        if messages[index].get("role") == "user":
            return index
    return -1


def _map_image_block_urls(messages: list[dict], transform) -> list[dict]:
    """Return a copy of *messages* with each ``image_url`` block's url passed through *transform*.

    ``transform`` returns a replacement url, or ``None`` to leave the block unchanged. Only messages that
    actually contain an image_url block are copied; the rest are shared by reference (cheap, safe: the
    caller never mutates in place)."""
    out: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list) or not any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in content
        ):
            out.append(message)
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url", "")
                replacement = transform(url)
                if replacement is not None:
                    block = {**block, "image_url": {**block["image_url"], "url": replacement}}
            new_content.append(block)
        out.append({**message, "content": new_content})
    return out


def compact_message_images(messages: list[dict], images_path) -> list[dict]:
    """Rewrite inline base64 image data URLs to on-disk ``/images/<hash>`` references (for persistence).

    Keeps ``sessions.json`` small: the bytes are written under ``images_path`` (content-addressed) and the
    stored message keeps only the short reference. A url that is already a reference or an http URL is left
    as-is."""

    def to_reference(url: str):
        if url.startswith("data:"):
            return images.save_data_url(images_path, url)
        return None

    return _map_image_block_urls(messages, to_reference)


def expand_message_images(messages: list[dict], images_path) -> list[dict]:
    """Rewrite ``/images/<name>`` references back to base64 data URLs (before restoring into the agent).

    The model request must carry pixels (a localhost /images URL is not fetchable by the provider), so a
    reference is re-read from disk here. A reference whose file is missing is left unchanged rather than
    crashing the restore."""

    def to_data_url(url: str):
        if images.is_reference(url):
            return images.reference_to_data_url(images_path, url)
        return None

    return _map_image_block_urls(messages, to_data_url)
