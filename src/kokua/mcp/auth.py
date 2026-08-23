"""OAuth for MCP connections that surfaces the authorization link in the chat channel.

FastMCP's default OAuth flow opens the user's browser (``webbrowser.open``) and stores tokens
in memory (re-auth every process). For a chat assistant we want two changes:

- **Post the authorization URL into the conversation** as a clickable link, in addition to
  opening the browser. Robust if the auto-open no-ops (some environments) and the natural place
  for the user to look, since they asked the assistant to connect in chat.
- **Persist tokens to disk** so authorizing once survives restarts and reconnects are silent.
- **Let the callback be placed** (:class:`OAuthSettings`), since FastMCP's default assumes the
  browser runs on the same machine as the client and Kokua is often reached over a network.

``ChatOAuth`` is a thin ``fastmcp`` ``OAuth`` subclass wired with a persistent ``FileTreeStore``
and a ``notify`` callback; ``build_chat_oauth`` constructs one for a URL from an
:class:`OAuthSettings`. AIMU forwards the provider object straight to the underlying
``fastmcp.Client`` (see ``MCPClient(auth=...)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastmcp.client.auth.oauth import OAuth
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)

# Async callable that delivers a short message to the user (bound to a Channel.send).
Notify = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class OAuthSettings:
    """Where OAuth tokens are cached, and where the authorization redirect has to land.

    The two callback fields exist because FastMCP's defaults (``localhost`` and a fresh random port
    per process) assume the browser runs on the same machine as the client. When it does not, the
    provider sends the approved code to *the browser's* loopback, Kokua's listener never hears it,
    and the flow fails only when it times out minutes later. Both fields are one value serving two
    purposes, which is FastMCP's shape and worth knowing: ``callback_host`` is the interface the
    callback server binds *and* the host in the registered ``redirect_uri``, so it has to name the
    Kokua machine from the browser's point of view as well as from Kokua's.

    ``callback_port`` is worth pinning even on one machine. Client registration is cached across
    restarts while a random port is not, so a re-auth in a later process sends a ``redirect_uri``
    the provider has on file under a different port and can reject.
    """

    storage_dir: Path
    callback_host: str = "localhost"
    callback_port: Optional[int] = None


class ChatOAuth(OAuth):
    """OAuth provider that also posts the authorization link into the chat channel."""

    def __init__(self, mcp_url: str, *, notify: Notify, token_storage: FileTreeStore, **kwargs):
        super().__init__(mcp_url, token_storage=token_storage, **kwargs)
        self._notify = notify

    @property
    def _callback_url(self) -> str:
        """The address the provider will send the approved browser to.

        Read off the client metadata rather than rebuilt from the settings: this is the exact value
        the authorization request carries, including the port, which is chosen at bind time when the
        config does not pin one and is then the number the user most needs to be told. FastMCP's
        ``_bind`` always fills the list, and ``ChatOAuth`` always binds (its URL is required).
        """
        return str(self.context.client_metadata.redirect_uris[0])

    async def redirect_handler(self, authorization_url: str) -> None:
        """Post the auth URL to the chat, then run the default browser-open + pre-flight."""
        await self._notify(
            f"To connect, authorize access here: {authorization_url}\n"
            f"After you approve, your browser is sent to {self._callback_url}, where Kokua is "
            "listening, so that address has to reach the machine Kokua runs on. A browser window "
            "should also open there automatically."
        )
        # super() performs the stale-client pre-flight check and webbrowser.open(). Run it after
        # posting the link so the user always has the URL even if the browser does not open.
        await super().redirect_handler(authorization_url)


def build_chat_oauth(url: str, *, notify: Notify, oauth: OAuthSettings) -> ChatOAuth:
    """Build a ``ChatOAuth`` for ``url`` with file-backed token storage under ``oauth.storage_dir``.

    FastMCP keys cached tokens/client-info by the full server URL (e.g.
    ``https://host/mcp/client_info``). ``FileTreeStore`` defaults to no key sanitization, so those
    slashes/colons would be treated as nested directories that don't exist (``FileNotFoundError``).
    The library's V1 sanitization strategies collapse a key/collection to one safe filename, which
    is exactly what a URL-keyed store needs.
    """
    oauth.storage_dir.mkdir(parents=True, exist_ok=True)
    store = FileTreeStore(
        data_directory=str(oauth.storage_dir),
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory=oauth.storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory=oauth.storage_dir),
    )
    return ChatOAuth(
        url,
        notify=notify,
        token_storage=store,
        callback_host=oauth.callback_host,
        callback_port=oauth.callback_port,
    )
