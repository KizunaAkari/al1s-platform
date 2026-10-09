import asyncio
import base64
from io import BytesIO
from threading import Event
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from pydantic import SecretStr

from al1s.api.meme_workshop import router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.meme_workshop.contracts import Layout, RenderInputs, TemplateWrite
from al1s.meme_workshop.pool import RenderPool


def image_data() -> str:
    output = BytesIO()
    Image.new("RGB", (40, 40), "red").save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode()


async def administrator_preview_case() -> None:
    app = FastAPI()
    settings = Settings(_env_file=None, admin_password=SecretStr("meme-test-admin-password"))
    app.state.settings = settings
    app.state.meme_workshop = MagicMock()
    app.include_router(router, prefix="/api/v1")
    install_admin_auth(app, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (await client.get("/api/v1/bots/memes/templates")).status_code == 401
        login = await client.post(
            "/api/v1/auth/login",
            json={"password": "meme-test-admin-password"},
            headers={"X-AL1S-CSRF": "1"},
        )
        assert login.status_code == 200
        request = {
            "template": TemplateWrite(
                name="预览", keyword="预览", layout=Layout(engine="petpet", width=128, height=128)
            ).model_dump(mode="json"),
            "avatars": [image_data()],
            "images": [],
            "texts": [],
        }
        assert (await client.post("/api/v1/bots/memes/preview", json=request)).status_code == 403
        from al1s.meme_workshop.renderer import render

        app.state.meme_workshop.preview.return_value = render(
            Layout(engine="petpet", width=128, height=128),
            RenderInputs(avatars=[base64.b64decode(image_data())]),
            {},
        )
        response = await client.post(
            "/api/v1/bots/memes/preview", json=request, headers={"X-AL1S-CSRF": "1"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/gif"
        assert response.headers["cache-control"] == "no-store"
        assert response.content.startswith(b"GIF")
        app.state.meme_workshop.repository.start_send.assert_not_called()


async def worker_authorization_case() -> None:
    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    app.state.bot_service = MagicMock()
    identity = uuid4()
    app.state.bot_service.worker_service_id.return_value = identity
    app.state.meme_workshop = MagicMock()
    app.state.meme_workshop.catalog.return_value = {"enabled": False, "items": []}
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/bots/worker/memes/catalog", headers={"Authorization": "Bearer worker-test"}
        )
        assert response.status_code == 200
        app.state.meme_workshop.catalog.assert_called_once_with(identity)
        response = await client.post(
            "/api/v1/bots/worker/memes/claim",
            json={
                "bot_service_id": str(uuid4()),
                "source": {
                    "message_id": "123",
                    "scope_kind": "discord_channel",
                    "target_id": "1234567890123456789",
                    "sender_id": "2234567890123456789",
                },
            },
            headers={"Authorization": "Bearer worker-test"},
        )
        assert response.status_code == 422


def test_busy_renderer_keeps_the_slot_until_the_real_work_finishes() -> None:
    pool = RenderPool(limit=2)
    started, release = Event(), Event()

    def blocked():
        started.set()
        assert release.wait(3)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(pool.run, blocked), executor.submit(pool.run, blocked)
        assert started.wait(1)
        import time

        deadline = time.monotonic() + 1
        while pool.active < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        with pytest.raises(ValueError, match="render_busy"):
            pool.run(lambda: None)
        release.set()
        first.result()
        second.result()
    assert pool.active == 0
    pool.run(lambda: None)


def test_administrator_preview_is_protected_and_never_sends_to_chat() -> None:
    asyncio.run(administrator_preview_case())


def test_worker_cannot_choose_another_connection_or_mutate_templates() -> None:
    asyncio.run(worker_authorization_case())
