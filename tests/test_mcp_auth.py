"""Unit tests for ChatOAuth: it posts the authorization link into the chat channel."""

from pathlib import Path

from fastmcp.client.auth.oauth import OAuth

from kokua.mcp_auth import ChatOAuth, build_chat_oauth


async def test_build_chat_oauth_creates_storage_and_provider(tmp_path: Path):
    posted: list[str] = []

    async def notify(message: str) -> None:
        posted.append(message)

    storage = tmp_path / "oauth"
    provider = build_chat_oauth("https://svc/mcp", notify=notify, token_storage_dir=storage)

    assert isinstance(provider, ChatOAuth)
    assert storage.exists()  # file-backed token storage directory created up front


async def test_token_storage_survives_url_shaped_keys(tmp_path: Path):
    """Regression: FastMCP keys storage by the full server URL; the store must not treat the
    slashes/colons as nested directories (the original FileNotFoundError)."""
    posted: list[str] = []

    async def notify(message: str) -> None:
        posted.append(message)

    provider = build_chat_oauth(
        "https://agent.robinhood.com/mcp/trading", notify=notify, token_storage_dir=tmp_path / "oauth"
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

    provider = build_chat_oauth("https://svc/mcp", notify=notify, token_storage_dir=tmp_path / "oauth")
    await provider.redirect_handler("https://auth.svc/authorize?x=1")

    assert len(posted) == 1
    assert "https://auth.svc/authorize?x=1" in posted[0]
    assert opened == ["https://auth.svc/authorize?x=1"]  # browser-open path still runs after the chat post
