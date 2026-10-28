import asyncio
from types import SimpleNamespace
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
    app.state.services = SimpleNamespace()
    settings = Settings(_env_file=None, admin_password=SecretStr("meme-proxy-test-password"))
    app.state.settings = settings
    app.state.services.settings = app.state.settings
    app.state.services.meme_backend = MagicMock()
    app.state.services.meme_backend.forward.return_value = BackendResponse(
        200,
        b"GIF89a",
        "image/gif",
    )
    app.state.services.bot_service = MagicMock()
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
        assert app.state.services.meme_backend.forward.call_count == 1
        response = await client.get(
            "/api/v1/bots/worker/memes/catalog",
            headers={
                "Authorization": "Bearer worker-test-credential",
            },
        )
        assert response.status_code == 200
        app.state.services.bot_service.worker_service_id.assert_called_once_with("worker-test-credential")
        assert (await client.get("/api/v1/bots/memes/private-files")).status_code == 404


async def quant_proxy_case() -> None:
    app = FastAPI()
    settings = Settings(_env_file=None, admin_password=SecretStr("quant-proxy-test-password"))
    app.state.settings = settings
    qq, discord = MagicMock(), MagicMock()
    qq.forward.return_value = BackendResponse(200, b"{}", "application/json")
    app.state.services = SimpleNamespace(settings=settings, meme_backends={"qq": qq, "discord": discord})
    app.include_router(router, prefix="/api/v1")
    install_admin_auth(app, settings)
    path = "/api/v1/bots/quant/settings/00000000-0000-0000-0000-000000000001"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        assert (await client.get(path)).status_code == 401
        await client.post("/api/v1/auth/login", json={"password": "quant-proxy-test-password"},
                          headers={"X-AL1S-CSRF": "1"})
        assert (await client.put(path, content=b"{}")).status_code == 403
        response = await client.put(path, content=b"{}", headers={"X-AL1S-CSRF": "1",
                                    "If-Match": "0", "Idempotency-Key": "k1"})
        assert response.status_code == 200
        assert qq.forward.call_args.args[1] == path
        discord.forward.assert_not_called()
        assert (await client.get(path + "?untrusted=x")).status_code == 400
        assert (await client.get("/api/v1/bots/quant/status")).status_code == 200
        assert qq.forward.call_args.args[1] == "/api/v1/bots/quant/status"
        assert (await client.put("/api/v1/bots/quant/credentials/tushare", json={"secret": "private"})).status_code == 403
        assert (await client.put("/api/v1/bots/quant/credentials/tushare", json={"secret": "private"}, headers={"X-AL1S-CSRF": "1"})).status_code == 200
        assert qq.forward.call_args.args[1] == "/api/v1/bots/quant/credentials/tushare"
        assert (await client.get("/api/v1/bots/quant/credentials")).status_code == 200
        discord.forward.assert_not_called()


def test_quant_proxy_uses_admin_auth_csrf_and_only_qq_backend() -> None:
    asyncio.run(quant_proxy_case())


def test_platform_authenticates_and_forwards_without_running_image_generation() -> None:
    asyncio.run(proxy_case())
