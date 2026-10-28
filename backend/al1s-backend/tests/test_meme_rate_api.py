import asyncio
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from al1s.api.meme_proxy import ADMIN_PATH, WORKER_PATH
from al1s.api.meme_workshop import router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings


async def rate_api_case():
    app = FastAPI()
    settings = Settings(_env_file=None, admin_password=SecretStr("rate-test-admin-password"))
    app.state.settings = settings
    app.state.meme_workshop = MagicMock()
    repo = app.state.meme_workshop.repository
    repo.get_rate_limits.return_value = [
        {"platform": "discord", "interval_seconds": 5, "row_version": 1},
        {"platform": "qq", "interval_seconds": 10, "row_version": 1},
    ]
    repo.save_rate_limit.return_value = {
        "platform": "discord",
        "interval_seconds": 7,
        "row_version": 2,
    }
    app.include_router(router, prefix="/api/v1")
    install_admin_auth(app, settings)
    path = "/api/v1/bots/memes/rate-limits"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        await client.post(
            "/api/v1/auth/login",
            json={"password": "rate-test-admin-password"},
            headers={"X-AL1S-CSRF": "1"},
        )
        assert (await client.get(path)).json()[0]["interval_seconds"] == 5
        assert (
            await client.put(
                path + "/discord", json={"interval_seconds": 7}, headers={"If-Match": "1"}
            )
        ).status_code == 403
        headers = {"If-Match": "1", "X-AL1S-CSRF": "1"}
        response = await client.put(
            path + "/discord", json={"interval_seconds": 7}, headers=headers
        )
        assert response.status_code == 200 and response.json()["row_version"] == 2
        repo.save_rate_limit.assert_called_once()
        for invalid in (0, 3601, True, 1.5, "5"):
            assert (
                await client.put(path + "/qq", json={"interval_seconds": invalid}, headers=headers)
            ).status_code == 422
        repo.save_rate_limit.side_effect = ValueError("rate_limit_version_conflict")
        assert (
            await client.put(path + "/discord", json={"interval_seconds": 8}, headers=headers)
        ).status_code == 409
        assert (
            await client.put(path + "/smtp", json={"interval_seconds": 7}, headers=headers)
        ).status_code == 422


def test_rate_limit_management_requires_admin_csrf_version_and_strict_integer():
    asyncio.run(rate_api_case())


def test_platform_proxy_exposes_only_the_two_admin_platforms_and_no_worker_settings():
    assert ADMIN_PATH.fullmatch("rate-limits")
    assert ADMIN_PATH.fullmatch("rate-limits/discord")
    assert ADMIN_PATH.fullmatch("rate-limits/qq")
    assert not ADMIN_PATH.fullmatch("rate-limits/smtp")
    assert not WORKER_PATH.fullmatch("rate-limits")
