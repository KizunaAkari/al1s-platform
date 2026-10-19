import asyncio
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI

from al1s.api.meme_proxy import router
from al1s.meme_workshop.transport import BackendResponse


def test_each_platform_uses_only_its_own_backend():
    async def run():
        app = FastAPI()
        qq, discord = MagicMock(), MagicMock()
        qq.forward.return_value = BackendResponse(200, b"QQ", "image/png")
        discord.forward.return_value = BackendResponse(200, b"DC", "image/png")
        app.state.meme_backends = {"qq": qq, "discord": discord}
        app.include_router(router, prefix="/api/v1")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post("/api/v1/bots/memes/qq/preview", content=b"{}")
            assert result.status_code == 200 and result.content == b"QQ"
            discord.forward.assert_not_called()
            qq.forward.side_effect = OSError()
            assert (
                await client.post("/api/v1/bots/memes/qq/preview", content=b"{}")
            ).status_code == 503
            discord.forward.assert_not_called()
            assert (await client.get("/api/v1/bots/memes/other/runtime")).status_code == 404

    asyncio.run(run())
