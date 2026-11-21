"""Unit tests for ChatOAuth: it posts the authorization link into the chat channel."""

import errno
import socket
from pathlib import Path

import pytest
from fastmcp.client.auth.oauth import OAuth

from kokua.mcp import auth
from kokua.mcp.auth import CallbackUnavailable, ChatOAuth, OAuthSettings, build_chat_oauth


async def _ignore_retire(group: str) -> None: ...


class _Alerts:
    """The two halves of the alert surface ``ChatOAuth`` is handed, recorded in the order they happen."""

    def __init__(self):
        self.events: list[tuple] = []

    async def notify(self, message: str, *, url: str | None = None, group: str | None = None) -> None:
        self.events.append(("raised", message, url, group))

    async def retire(self, group: str) -> None:
        self.events.append(("retired", group))

    def build(self, tmp_path: Path, url: str = "https://svc/mcp", **settings) -> ChatOAuth:
        return build_chat_oauth(
            url,
            notify=self.notify,
            retire_alert=self.retire,
            oauth=OAuthSettings(storage_dir=tmp_path / "oauth", **settings),
        )


async def test_build_chat_oauth_creates_storage_and_provider(tmp_path: Path):
    posted: list[str] = []

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None:
        posted.append(message)

    storage = tmp_path / "oauth"
    provider = build_chat_oauth(
        "https://svc/mcp", notify=notify, retire_alert=_ignore_retire, oauth=OAuthSettings(storage_dir=storage)
    )

    assert isinstance(provider, ChatOAuth)
    assert storage.exists()  # file-backed token storage directory created up front


async def test_token_storage_survives_url_shaped_keys(tmp_path: Path):
    """Regression: FastMCP keys storage by the full server URL; the store must not treat the
    slashes/colons as nested directories (the original FileNotFoundError)."""
    posted: list[str] = []

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None:
        posted.append(message)

    provider = build_chat_oauth(
        "https://agent.robinhood.com/mcp/trading",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
    )
    store = provider._token_storage

    # The exact key shape FastMCP uses for client info (server_url + "/client_info").
    key = "https://agent.robinhood.com/mcp/trading/client_info"
    await store.put(key=key, value={"client_id": "abc"}, collection="mcp-oauth-client-info")
    assert await store.get(key=key, collection="mcp-oauth-client-info") == {"client_id": "abc"}


async def test_redirect_handler_posts_link_then_opens_browser(tmp_path: Path, monkeypatch):
    posted: list[str] = []

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None:
        posted.append(message)

    # Stub the parent's network pre-flight + webbrowser.open so the test is hermetic.
    opened: list[str] = []

    async def fake_super_redirect(self, authorization_url: str) -> None:
        opened.append(authorization_url)

    monkeypatch.setattr(OAuth, "redirect_handler", fake_super_redirect)

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
    )
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    assert len(posted) == 1
    assert "https://auth.svc/authorize?x=1" in posted[0]
    assert opened == ["https://auth.svc/authorize?x=1"]  # browser-open path still runs after the chat post


async def test_redirect_handler_passes_the_url_as_a_field(tmp_path: Path, monkeypatch):
    """The sentence still carries the link, for a channel that can only print one. The field beside it
    is what an alert card turns into a real anchor: a card renders its text verbatim rather than
    hunting for URLs in it, which is what keeps a model-written sentence from becoming a link."""
    posted: list[tuple[str, str | None]] = []

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None:
        posted.append((message, url))

    async def fake_super_redirect(self, authorization_url: str) -> None: ...

    monkeypatch.setattr(OAuth, "redirect_handler", fake_super_redirect)

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
    )
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    (message, url) = posted[0]
    assert url == "https://auth.svc/authorize?x=1"
    assert "https://auth.svc/authorize?x=1" in message


def test_default_callback_is_a_loopback_url(tmp_path: Path):
    """With nothing configured, FastMCP's own default: loopback host, an arbitrary free port."""

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None: ...

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
    )

    assert [str(uri) for uri in provider.context.client_metadata.redirect_uris] == [
        f"http://localhost:{provider.redirect_port}/callback"
    ]


def test_configured_callback_host_and_port_shape_the_redirect_uri(tmp_path: Path):
    """The fix for a Kokua running on a different machine than the browser: the redirect URI, and the
    listener behind it, are the user's to place."""

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None: ...

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth", callback_host="kokua.lan", callback_port=8765),
    )

    assert provider.redirect_port == 8765
    assert [str(uri) for uri in provider.context.client_metadata.redirect_uris] == ["http://kokua.lan:8765/callback"]


async def test_redirect_handler_names_the_callback_url(tmp_path: Path, monkeypatch):
    """The posted link says where the approval lands, because a redirect to the wrong machine is
    otherwise silent until the flow times out."""
    posted: list[str] = []

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None:
        posted.append(message)

    async def fake_super_redirect(self, authorization_url: str) -> None: ...

    monkeypatch.setattr(OAuth, "redirect_handler", fake_super_redirect)
    # kokua.lan resolves nowhere the suite runs, so the real bind check would refuse it.
    monkeypatch.setattr(auth, "_bind_error", lambda host, port: None)

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth", callback_host="kokua.lan", callback_port=8765),
    )
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    assert "http://kokua.lan:8765/callback" in posted[0]


@pytest.fixture
def held_port():
    """A loopback port another program is listening on, which is how the collision arrives in life."""
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        yield holder.getsockname()[1]


async def test_redirect_handler_refuses_a_callback_port_another_program_holds(tmp_path: Path, monkeypatch, held_port):
    """Regression: with the pinned port taken, the user was sent to approve, the provider delivered the
    code to whichever program held the port, and the failed bind then exited the whole process. The
    refusal comes before the link is posted or the browser opened, so nobody approves a doomed flow."""
    posted: list[tuple[str, str | None]] = []

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None:
        posted.append((message, url))

    opened: list[str] = []

    async def fake_super_redirect(self, authorization_url: str) -> None:
        opened.append(authorization_url)

    monkeypatch.setattr(OAuth, "redirect_handler", fake_super_redirect)

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth", callback_host="127.0.0.1", callback_port=held_port),
    )
    with pytest.raises(CallbackUnavailable) as excinfo:
        await provider.redirect_handler("https://auth.svc/authorize?x=1")

    assert opened == []
    assert len(posted) == 1
    (message, url) = posted[0]
    assert url is None and "https://auth.svc/authorize" not in message  # the link itself is never offered
    assert message == str(excinfo.value)
    assert f"127.0.0.1:{held_port}" in message
    assert "another program" in message and "oauth_callback_port" in message


async def test_callback_server_exit_becomes_callback_unavailable(tmp_path: Path, monkeypatch, held_port):
    """The backstop for a port taken between the check and the bind: uvicorn's sys.exit arrives as a
    SystemExit inside FastMCP's task group, and must surface as an ordinary error rather than end the
    process."""
    posted: list[str] = []

    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None:
        posted.append(message)

    async def exiting_callback_server(self):
        raise BaseExceptionGroup("unhandled errors in a TaskGroup", [SystemExit(1)])

    monkeypatch.setattr(OAuth, "callback_handler", exiting_callback_server)

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth", callback_host="127.0.0.1", callback_port=held_port),
    )
    with pytest.raises(CallbackUnavailable) as excinfo:
        await provider.callback_handler()

    assert isinstance(excinfo.value.cause, OSError)  # the bind is re-checked so the message can say why
    assert posted == [str(excinfo.value)]


async def test_callback_handler_passes_other_failures_through(tmp_path: Path, monkeypatch):
    async def notify(message: str, *, url: str | None = None, group: str | None = None) -> None: ...

    async def failing_callback_server(self):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("state mismatch")])

    monkeypatch.setattr(OAuth, "callback_handler", failing_callback_server)

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        retire_alert=_ignore_retire,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
    )
    with pytest.raises(ExceptionGroup):
        await provider.callback_handler()


def test_bind_error_ignores_a_free_port_and_reports_a_held_one(held_port):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        free_port = spare.getsockname()[1]
    assert auth._bind_error("127.0.0.1", free_port) is None
    assert auth._bind_error("127.0.0.1", held_port) is not None


def test_callback_unavailable_message_does_not_read_as_a_registration_failure():
    """connect_mcp turns any OAuth failure mentioning registration into a "provide a bearer token" reply,
    which would send the user after the wrong fix entirely."""
    from kokua.mcp.servers import _looks_like_registration_unsupported

    for cause in (
        OSError(errno.EADDRINUSE, "Address already in use"),
        OSError(errno.EADDRNOTAVAIL, "Cannot assign"),
        None,
    ):
        assert not _looks_like_registration_unsupported(CallbackUnavailable("localhost", 8765, cause))


# --- the authorization card leaves once nothing is waiting on it ----------------------------------

GROUP = "mcp-auth:https://svc/mcp"


@pytest.fixture
def quiet_browser(monkeypatch):
    """The parent's network pre-flight and webbrowser.open, stubbed so a test is hermetic."""

    async def fake_super_redirect(self, authorization_url: str) -> None: ...

    monkeypatch.setattr(OAuth, "redirect_handler", fake_super_redirect)


async def test_the_authorization_card_is_grouped_by_server(tmp_path: Path, quiet_browser):
    """The group is what lets the card be retired, and what makes a second flow for the same server
    replace the first card rather than stack beside it."""
    alerts = _Alerts()
    await alerts.build(tmp_path).redirect_handler("https://auth.svc/authorize?x=1")

    (event,) = alerts.events
    assert event[0] == "raised" and event[3] == GROUP


async def test_the_authorization_card_is_retired_once_the_callback_arrives(tmp_path: Path, monkeypatch, quiet_browser):
    """Regression: the card stayed after the user had approved, a link to a flow already finished.
    The callback arriving is the moment the user's part is done, whether they clicked the card or
    used the browser tab that opened on its own."""

    async def approved(self):
        return "code", "state"

    monkeypatch.setattr(OAuth, "callback_handler", approved)
    alerts = _Alerts()
    provider = alerts.build(tmp_path)
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    assert await provider.callback_handler() == ("code", "state")
    assert alerts.events[-1] == ("retired", GROUP)


async def test_an_abandoned_authorization_replaces_its_card_with_the_reason(tmp_path: Path, monkeypatch, quiet_browser):
    """A timed-out flow's link leads nowhere, so the card says what happened instead. FastMCP raises
    the timeout from inside its task group, which is why it arrives wrapped."""

    async def timed_out(self):
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup", [TimeoutError("OAuth callback timed out after 300 seconds")]
        )

    monkeypatch.setattr(OAuth, "callback_handler", timed_out)
    alerts = _Alerts()
    provider = alerts.build(tmp_path)
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    with pytest.raises(ExceptionGroup):
        await provider.callback_handler()

    (kind, message, url, group) = alerts.events[-1]
    assert (kind, url, group) == ("raised", None, GROUP)
    assert "https://svc/mcp" in message and "timed out after 300 seconds" in message
    assert "https://auth.svc/authorize" not in message  # the dead link is not offered again


async def test_a_refused_callback_port_replaces_the_card_in_its_group(tmp_path: Path, monkeypatch, held_port):
    async def exiting_callback_server(self):
        raise BaseExceptionGroup("unhandled errors in a TaskGroup", [SystemExit(1)])

    monkeypatch.setattr(OAuth, "callback_handler", exiting_callback_server)
    alerts = _Alerts()
    provider = alerts.build(tmp_path, callback_host="127.0.0.1", callback_port=held_port)

    with pytest.raises(CallbackUnavailable) as excinfo:
        await provider.callback_handler()

    assert alerts.events == [("raised", str(excinfo.value), None, GROUP)]


async def test_a_failed_preflight_retires_the_card_it_just_raised(tmp_path: Path, monkeypatch):
    """FastMCP's pre-flight runs after the card is posted, and its stale-client failure makes FastMCP
    start the flow over. The card it raised has a link to the abandoned attempt, so it goes."""

    async def stale_client(self, authorization_url: str) -> None:
        raise RuntimeError("OAuth client not found")

    monkeypatch.setattr(OAuth, "redirect_handler", stale_client)
    alerts = _Alerts()

    with pytest.raises(RuntimeError):
        await alerts.build(tmp_path).redirect_handler("https://auth.svc/authorize?x=1")

    assert [event[0] for event in alerts.events] == ["raised", "retired"]
    assert alerts.events[-1] == ("retired", GROUP)
