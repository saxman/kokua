"""Unit tests for ChatOAuth: it posts the authorization link into the chat channel."""

from pathlib import Path

from fastmcp.client.auth.oauth import OAuth

from kokua.mcp.auth import ChatOAuth, OAuthSettings, build_chat_oauth


async def test_build_chat_oauth_creates_storage_and_provider(tmp_path: Path):
    posted: list[str] = []

    async def notify(message: str) -> None:
        posted.append(message)

    storage = tmp_path / "oauth"
    provider = build_chat_oauth("https://svc/mcp", notify=notify, oauth=OAuthSettings(storage_dir=storage))

    assert isinstance(provider, ChatOAuth)
    assert storage.exists()  # file-backed token storage directory created up front


async def test_token_storage_survives_url_shaped_keys(tmp_path: Path):
    """Regression: FastMCP keys storage by the full server URL; the store must not treat the
    slashes/colons as nested directories (the original FileNotFoundError)."""
    posted: list[str] = []

    async def notify(message: str) -> None:
        posted.append(message)

    provider = build_chat_oauth(
        "https://agent.robinhood.com/mcp/trading", notify=notify, oauth=OAuthSettings(storage_dir=tmp_path / "oauth")
    )
    store = provider._token_storage

    # The exact key shape FastMCP uses for client info (server_url + "/client_info").
    key = "https://agent.robinhood.com/mcp/trading/client_info"
    await store.put(key=key, value={"client_id": "abc"}, collection="mcp-oauth-client-info")
    assert await store.get(key=key, collection="mcp-oauth-client-info") == {"client_id": "abc"}


async def test_redirect_handler_posts_link_then_opens_browser(tmp_path: Path, monkeypatch):
    posted: list[str] = []

    async def notify(message: str) -> None:
        posted.append(message)

    # Stub the parent's network pre-flight + webbrowser.open so the test is hermetic.
    opened: list[str] = []

    async def fake_super_redirect(self, authorization_url: str) -> None:
        opened.append(authorization_url)

    monkeypatch.setattr(OAuth, "redirect_handler", fake_super_redirect)

    provider = build_chat_oauth("https://svc/mcp", notify=notify, oauth=OAuthSettings(storage_dir=tmp_path / "oauth"))
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    assert len(posted) == 1
    assert "https://auth.svc/authorize?x=1" in posted[0]
    assert opened == ["https://auth.svc/authorize?x=1"]  # browser-open path still runs after the chat post


def test_default_callback_is_a_loopback_url(tmp_path: Path):
    """With nothing configured, FastMCP's own default: loopback host, an arbitrary free port."""

    async def notify(message: str) -> None: ...

    provider = build_chat_oauth("https://svc/mcp", notify=notify, oauth=OAuthSettings(storage_dir=tmp_path / "oauth"))

    assert [str(uri) for uri in provider.context.client_metadata.redirect_uris] == [
        f"http://localhost:{provider.redirect_port}/callback"
    ]


def test_configured_callback_host_and_port_shape_the_redirect_uri(tmp_path: Path):
    """The fix for a Kokua running on a different machine than the browser: the redirect URI, and the
    listener behind it, are the user's to place."""

    async def notify(message: str) -> None: ...

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth", callback_host="kokua.lan", callback_port=8765),
    )

    assert provider.redirect_port == 8765
    assert [str(uri) for uri in provider.context.client_metadata.redirect_uris] == ["http://kokua.lan:8765/callback"]


async def test_redirect_handler_names_the_callback_url(tmp_path: Path, monkeypatch):
    """The posted link says where the approval lands, because a redirect to the wrong machine is
    otherwise silent until the flow times out."""
    posted: list[str] = []

    async def notify(message: str) -> None:
        posted.append(message)

    async def fake_super_redirect(self, authorization_url: str) -> None: ...

    monkeypatch.setattr(OAuth, "redirect_handler", fake_super_redirect)

    provider = build_chat_oauth(
        "https://svc/mcp",
        notify=notify,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth", callback_host="kokua.lan", callback_port=8765),
    )
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    assert "http://kokua.lan:8765/callback" in posted[0]
