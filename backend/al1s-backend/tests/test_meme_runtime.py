import asyncio
from unittest.mock import MagicMock

import httpx

from al1s.meme_workshop.runtime import create_runtime_app


async def runtime_case() -> None:
    service = MagicMock()
    service.repository.list_templates.return_value = []
    app = create_runtime_app(service, MagicMock(), "runtime-control-test-token-at-least-32-bytes")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/api/v1/bots/memes/templates")).status_code == 401
        response = await client.get(
            "/api/v1/bots/memes/templates",
            headers={
                "X-AL1S-Meme-Control": "runtime-control-test-token-at-least-32-bytes",
            },
        )
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.headers["cache-control"] == "no-store"


def test_bot_backend_requires_the_private_platform_control_token() -> None:
    asyncio.run(runtime_case())
