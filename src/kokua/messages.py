"""Transcript helpers: message text/title derivation and image-block url rewriting.

Pure functions with no `Assistant` coupling, split out of the assistant core. The image helpers
bridge AIMU's inline base64 message content and Kokua's on-disk `/images/<name>` store (see
`images.py`): compact on persist, expand before `agent.restore`.
"""

from __future__ import annotations

from typing import Optional

from . import images


def message_text(content) -> str:
    """Plain text of a message's content (a string, or the text blocks of a multimodal list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def derive_title(messages: list[dict]) -> Optional[str]:
    """A conversation title from the first user message (stripped, truncated), or None."""
    for message in messages:
        if message.get("role") == "user":
            text = message_text(message.get("content")).strip()
            if text:
                return text[:40]
    return None


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
