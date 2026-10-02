"""Discord application identity lookup never stores or exposes the Bot Token."""

from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api import bots as bots_api
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.bots import discord_identity
from al1s.bots.errors import BotDomainError

TOKEN = "synthetic.bot.token.with.more.than.thirty.characters"
APPLICATION_ID = "1234567890123456789"


class FakeOpener:
    def __init__(self, body: bytes | Exception) -> None:
        self.body = body

    def open(self, request: Request, timeout: int) -> BytesIO:
        assert timeout == 6
        assert request.full_url == "https://discord.com/api/v10/oauth2/applications/@me"
        assert request.get_header("Authorization") == f"Bot {TOKEN}"
        if isinstance(self.body, Exception):
            raise self.body
        return BytesIO(self.body)


def test_lookup_uses_fixed_endpoint_without_proxy_or_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def opener(*handlers: object) -> FakeOpener:
        assert isinstance(handlers[0], ProxyHandler)
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], discord_identity._NoRedirect)
        return FakeOpener(b'{"id":"1234567890123456789"}')

    monkeypatch.setattr(discord_identity, "build_opener", opener)
    assert discord_identity.resolve_discord_application_id(TOKEN) == APPLICATION_ID


@pytest.mark.parametrize(
    ("body", "code", "status"),
    [
        (HTTPError("https://discord.com", 401, TOKEN, {}, None), "discord_token_rejected", 422),
        (HTTPError("https://discord.com", 429, TOKEN, {}, None), "discord_rate_limited", 429),
        (URLError(TOKEN), "discord_lookup_unavailable", 503),
        (b"not json", "discord_lookup_failed", 502),
        (b'{"id":"invalid"}', "discord_lookup_failed", 502),
        (b"x" * 65_537, "discord_lookup_failed", 502),
    ],
    ids=["unauthorized", "rate-limit", "unavailable", "bad-json", "bad-id", "oversized"],
)
def test_lookup_failures_do_not_expose_token(
    monkeypatch: pytest.MonkeyPatch, body: bytes | Exception, code: str, status: int
) -> None:
    monkeypatch.setattr(discord_identity, "build_opener", lambda *handlers: FakeOpener(body))
    with pytest.raises(BotDomainError) as caught:
        discord_identity.resolve_discord_application_id(TOKEN)
    assert caught.value.code == code
    assert caught.value.status_code == status
    assert TOKEN not in str(caught.value)


def test_lookup_api_requires_admin_and_csrf_and_does_not_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bots_api, "resolve_discord_application_id", lambda token: APPLICATION_ID)
    app = FastAPI()
    settings = Settings(admin_password="test-admin-password", admin_cookie_secure=False)
    app.state.settings = settings
    install_admin_auth(app, settings)
    app.include_router(bots_api.router, prefix="/api/v1/bots")
    path = "/api/v1/bots/discord/application-id"
    with TestClient(app) as client:
        body = {"token": TOKEN}
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json=body, headers={"X-AL1S-CSRF": "1"}).status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"password": "test-admin-password"},
            headers={"X-AL1S-CSRF": "1"},
        )
        assert login.status_code == 200
        response = client.post(path, json=body, headers={"X-AL1S-CSRF": "1"})
        assert response.status_code == 200
        assert response.json() == {"application_id": APPLICATION_ID}
        assert response.headers["cache-control"] == "no-store"
        assert TOKEN not in response.text
