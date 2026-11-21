"""OAuth for MCP connections that surfaces the authorization link in the chat channel.

FastMCP's default OAuth flow opens the user's browser (``webbrowser.open``) and stores tokens
in memory (re-auth every process). For a chat assistant we want two changes:

- **Post the authorization URL into the conversation** as a clickable link, in addition to
  opening the browser. Robust if the auto-open no-ops (some environments) and the natural place
  for the user to look, since they asked the assistant to connect in chat.
- **Persist tokens to disk** so authorizing once survives restarts and reconnects are silent.
- **Let the callback be placed** (:class:`OAuthSettings`), since FastMCP's default assumes the
  browser runs on the same machine as the client and Kokua is often reached over a network.
- **Refuse a callback address Kokua cannot listen on** (:class:`CallbackUnavailable`), before the
  link is posted. FastMCP binds the callback server only after the browser has been sent off, and
  a failed bind exits through uvicorn's ``sys.exit``, which took the whole process down with it.
- **Take the link back once nothing is waiting on it.** An alert card stays until something
  replaces or retires it, so the card is retired when the callback arrives and replaced by the
  reason when the flow is abandoned. Left up, it offers a link to a flow already finished.

``ChatOAuth`` is a thin ``fastmcp`` ``OAuth`` subclass wired with a persistent ``FileTreeStore``
and the ``notify`` / ``retire_alert`` pair; ``build_chat_oauth`` constructs one for a URL from an
:class:`OAuthSettings`. AIMU forwards the provider object straight to the underlying
``fastmcp.Client`` (see ``MCPClient(auth=...)``).
"""

from __future__ import annotations

import asyncio
import errno
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urlsplit

from fastmcp.client.auth.oauth import OAuth
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)


class Notify(Protocol):
    """Post a line the user needs to see, outside whatever conversation they are reading.

    ``url`` is the one thing in the line worth clicking, passed as a field as well as in the text:
    a front end that can render a control makes one from it, and a plain-text channel still has the
    address in the sentence. ``group`` names what the line is about, so a later line, or a
    :class:`RetireAlert` call, for the same group replaces it. See ``ChannelUI.alert``, which is what
    this is bound to.
    """

    async def __call__(self, text: str, *, url: Optional[str] = None, group: Optional[str] = None) -> None: ...


class RetireAlert(Protocol):
    """Take back the line :class:`Notify` posted under ``group``, because nothing waits on it any more.

    See ``ChannelUI.retire_alert``, which is what this is bound to. A channel that printed the line
    cannot take it back, so there it does nothing.
    """

    async def __call__(self, group: str) -> None: ...


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


class CallbackUnavailable(Exception):
    """Kokua cannot listen at the address the provider will send the approved browser to.

    Its message is user-facing and actionable. ``cause`` is the bind failure, or ``None`` when the
    callback server failed to start without saying why (uvicorn logs the error and exits instead).
    The common case is a pinned ``oauth_callback_port`` that another program on the machine already
    holds, and it is worse than a failed connection: the approved browser lands on *that* program,
    which receives the authorization code.
    """

    def __init__(self, host: str, port: int, cause: Optional[OSError]):
        if cause is not None and cause.errno != errno.EADDRINUSE:
            message = (
                f"Kokua cannot listen for the OAuth approval on {host}:{port} ({cause.strerror or cause}). "
                "Check [mcp].oauth_callback_host and oauth_callback_port in config.toml, then connect again."
            )
        else:
            reason = (
                "another program is already listening there"
                if cause is not None
                else "its callback server failed to start"
            )
            message = (
                f"Kokua cannot receive the OAuth approval on {host}:{port}: {reason}. Set "
                "[mcp].oauth_callback_port in config.toml to a free port (and forward that port instead, if "
                "you reach Kokua over SSH), then connect again."
            )
        super().__init__(message)
        self.host = host
        self.port = port
        self.cause = cause


def _bind_error(host: str, port: int) -> Optional[OSError]:
    """Bind ``host:port`` the way the callback server will, and return why it cannot, or ``None``.

    Every address the host resolves to is tried, because asyncio's ``create_server`` binds them all
    and a conflict on any one (``::1`` as well as ``127.0.0.1`` for ``localhost``) fails the whole
    server. ``SO_REUSEADDR`` matches what asyncio sets, so a socket lingering in ``TIME_WAIT`` from a
    previous flow does not read as taken; a live listener still does.
    """
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError as exc:
        return exc
    for family, kind, protocol, _, address in addresses:
        with socket.socket(family, kind, protocol) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                probe.bind(address)
            except OSError as exc:
                return exc
    return None


class ChatOAuth(OAuth):
    """OAuth provider that also posts the authorization link into the chat channel.

    Every line it posts carries one group per server, so the link, the reason a flow could not
    finish, and the retirement all address the same card, and a second flow for the server (FastMCP
    restarts one whose cached client the provider no longer knows) replaces the first card.
    """

    def __init__(
        self, mcp_url: str, *, notify: Notify, retire_alert: RetireAlert, token_storage: FileTreeStore, **kwargs
    ):
        super().__init__(mcp_url, token_storage=token_storage, **kwargs)
        self._notify = notify
        self._retire_alert = retire_alert
        self._mcp_url = mcp_url
        self._alert_group = f"mcp-auth:{mcp_url}"

    @property
    def _callback_url(self) -> str:
        """The address the provider will send the approved browser to.

        Read off the client metadata rather than rebuilt from the settings: this is the exact value
        the authorization request carries, including the port, which is chosen at bind time when the
        config does not pin one and is then the number the user most needs to be told. FastMCP's
        ``_bind`` always fills the list, and ``ChatOAuth`` always binds (its URL is required).
        """
        return str(self.context.client_metadata.redirect_uris[0])

    @property
    def _callback_address(self) -> tuple[str, int]:
        """The host and port the callback server binds, taken from :attr:`_callback_url` so the check
        and the redirect cannot disagree. ``hostname`` drops an IPv6 host's brackets, as a bind needs."""
        callback = urlsplit(self._callback_url)
        return callback.hostname or "", callback.port

    async def _callback_unavailable(self, cause: Optional[OSError]) -> CallbackUnavailable:
        """Tell the user why authorization cannot finish, and return the error to raise."""
        error = CallbackUnavailable(*self._callback_address, cause)
        await self._notify(str(error), group=self._alert_group)
        return error

    async def redirect_handler(self, authorization_url: str) -> None:
        """Check the callback can be received, post the auth URL to the chat, then run the default
        browser-open + pre-flight.

        The check comes first because FastMCP binds the callback server only after this returns, by
        which point the user has been sent to approve a flow whose code nothing here can receive.
        """
        cause = await asyncio.to_thread(_bind_error, *self._callback_address)
        if cause is not None:
            raise await self._callback_unavailable(cause)
        await self._notify(
            f"To connect, authorize access here: {authorization_url}\n"
            f"After you approve, your browser is sent to {self._callback_url}, where Kokua is "
            "listening, so that address has to reach the machine Kokua runs on. A browser window "
            "should also open there automatically.",
            url=authorization_url,
            group=self._alert_group,
        )
        # super() performs the stale-client pre-flight check and webbrowser.open(). Run it after
        # posting the link so the user always has the URL even if the browser does not open. A failed
        # pre-flight abandons this attempt (and a stale client makes FastMCP start another, which
        # posts its own card), so the link just posted leads nowhere.
        try:
            await super().redirect_handler(authorization_url)
        except Exception:
            await self._retire_alert(self._alert_group)
            raise

    async def callback_handler(self) -> tuple[str, Optional[str]]:
        """Run FastMCP's callback server, then retire the authorization card or say why it failed.

        The callback arriving is where the user's part ends, so the card goes then rather than after
        the token exchange that follows. That exchange can still fail, and is reported where any
        connect failure is (the log at boot, the tool's result at runtime), because by then there is
        nothing the card could ask the user to do.

        A failure to start the server becomes :class:`CallbackUnavailable`. The check in
        :meth:`redirect_handler` makes this rare (something has to take the port in the moment between
        the two), but its cost is the whole process: uvicorn reports a failed bind by calling
        ``sys.exit(1)``, and the ``SystemExit`` passes every ``except Exception`` between here and the
        event loop. Within this call the callback server is the only thing that can raise one.

        Every failure arrives wrapped in an exception group, the timeout included, because FastMCP
        raises from inside the task group running the server.
        """
        try:
            result = await super().callback_handler()
        except BaseExceptionGroup as group:
            if group.subgroup(SystemExit) is not None:
                cause = await asyncio.to_thread(_bind_error, *self._callback_address)
                raise await self._callback_unavailable(cause) from group
            # Only an ordinary failure gets a card. A cancellation is the connect being torn down
            # (shutdown, a stopped turn), with no reason worth telling the user and no time to tell it.
            if isinstance(group, ExceptionGroup):
                await self._notify(
                    f"Authorization for {self._mcp_url} did not complete: {_innermost(group)}",
                    group=self._alert_group,
                )
            raise
        await self._retire_alert(self._alert_group)
        return result


def _innermost(group: BaseExceptionGroup) -> BaseException:
    """The first exception a (possibly nested) group wraps, which is the one worth telling the user."""
    first = group.exceptions[0]
    return _innermost(first) if isinstance(first, BaseExceptionGroup) else first


def build_chat_oauth(url: str, *, notify: Notify, retire_alert: RetireAlert, oauth: OAuthSettings) -> ChatOAuth:
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
        retire_alert=retire_alert,
        token_storage=store,
        callback_host=oauth.callback_host,
        callback_port=oauth.callback_port,
    )
