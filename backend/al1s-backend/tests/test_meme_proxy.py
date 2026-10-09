import asyncio
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from al1s.api.meme_proxy import router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.meme_workshop.transport import BackendResponse


async def proxy_case() -> None:
    app = FastAPI()
    settings = Settings(_env_file=None, admin_password=SecretStr("meme-proxy-test-password"))
    app.state.settings = settings
    app.state.meme_backend = MagicMock()
    app.state.meme_backend.forward.return_value = BackendResponse(
        200,
        b"GIF89a",
        "image/gif",
    )
    app.state.bot_service = MagicMock()
    app.include_router(router, prefix="/api/v1")
    install_admin_auth(app, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (
            await client.post(
                "/api/v1/bots/memes/preview", content=b"{}", headers={"X-AL1S-CSRF": "1"}
            )
        ).status_code == 401
        await client.post(
            "/api/v1/auth/login",
            json={"password": "meme-proxy-test-password"},
            headers={"X-AL1S-CSRF": "1"},
        )
        response = await client.post(
            "/api/v1/bots/memes/preview",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-AL1S-CSRF": "1",
            },
        )
        assert response.status_code == 200
        assert response.content == b"GIF89a"
        assert response.headers["content-type"] == "image/gif"
        assert response.headers["cache-control"] == "no-store"
        assert app.state.meme_backend.forward.call_count == 1
        response = await client.get(
            "/api/v1/bots/worker/memes/catalog",
            headers={
                "Authorization": "Bearer worker-test-credential",
            },
        )
        assert response.status_code == 200
        app.state.bot_service.worker_service_id.assert_called_once_with("worker-test-credential")
        assert (await client.get("/api/v1/bots/memes/private-files")).status_code == 404


def test_platform_authenticates_and_forwards_without_running_image_generation() -> None:
    asyncio.run(proxy_case())
