"""OAuth for MCP connections that surfaces the authorization link in the chat channel.

FastMCP's default OAuth flow opens the user's browser (``webbrowser.open``) and stores tokens
in memory (re-auth every process). For a chat assistant we want two changes:

- **Post the authorization URL into the conversation** as a clickable link, in addition to
  opening the browser. Robust if the auto-open no-ops (some environments) and the natural place
  for the user to look, since they asked the assistant to connect in chat.
- **Persist tokens to disk** so authorizing once survives restarts and reconnects are silent.

``ChatOAuth`` is a thin ``fastmcp`` ``OAuth`` subclass wired with a persistent ``FileTreeStore``
and a ``notify`` callback; ``build_chat_oauth`` constructs one for a URL. AIMU forwards the
provider object straight to the underlying ``fastmcp.Client`` (see ``MCPClient(auth=...)``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from fastmcp.client.auth.oauth import OAuth
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)

# Async callable that delivers a short message to the user (bound to a Channel.send).
Notify = Callable[[str], Awaitable[None]]


class ChatOAuth(OAuth):
    """OAuth provider that also posts the authorization link into the chat channel."""

    def __init__(self, mcp_url: str, *, notify: Notify, token_storage: FileTreeStore, **kwargs):
        super().__init__(mcp_url, token_storage=token_storage, **kwargs)
        self._notify = notify

    async def redirect_handler(self, authorization_url: str) -> None:
        """Post the auth URL to the chat, then run the default browser-open + pre-flight."""
        await self._notify(
            f"To connect, authorize access here: {authorization_url}\n"
            "A browser window should also open automatically. After you approve, the connection "
            "completes and the tools become available."
        )
        # super() performs the stale-client pre-flight check and webbrowser.open(). Run it after
        # posting the link so the user always has the URL even if the browser does not open.
        await super().redirect_handler(authorization_url)


def build_chat_oauth(url: str, *, notify: Notify, token_storage_dir: Path) -> ChatOAuth:
    """Build a ``ChatOAuth`` for ``url`` with file-backed token storage under ``token_storage_dir``.

    FastMCP keys cached tokens/client-info by the full server URL (e.g.
    ``https://host/mcp/client_info``). ``FileTreeStore`` defaults to no key sanitization, so those
    slashes/colons would be treated as nested directories that don't exist (``FileNotFoundError``).
    The library's V1 sanitization strategies collapse a key/collection to one safe filename, which
    is exactly what a URL-keyed store needs.
    """
    token_storage_dir.mkdir(parents=True, exist_ok=True)
    store = FileTreeStore(
        data_directory=str(token_storage_dir),
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory=token_storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory=token_storage_dir),
    )
    return ChatOAuth(url, notify=notify, token_storage=store)
