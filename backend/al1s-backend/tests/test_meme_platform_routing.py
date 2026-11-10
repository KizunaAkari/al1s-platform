import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI

from al1s.api.meme_proxy import router
from al1s.meme_workshop.transport import BackendResponse


def test_each_platform_uses_only_its_own_backend():
    async def run():
        app = FastAPI()
        app.state.services = SimpleNamespace()
        qq, discord = MagicMock(), MagicMock()
        qq.forward.return_value = BackendResponse(200, b"QQ", "image/png")
        discord.forward.return_value = BackendResponse(200, b"DC", "image/png")
        app.state.services.meme_backends = {"qq": qq, "discord": discord}
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


def test_drawing_timeout_preserves_existing_ai_provider_test_budget(monkeypatch):
    from al1s.meme_workshop import transport
    timeouts = []
    class Response:
        status = 200
        def getheader(self, _name, default=None): return default
        def read(self, _limit): return b'{}'
    class Connection:
        def __init__(self, _host, _port, timeout): timeouts.append(timeout)
        def request(self, *_args, **_kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(transport, 'HTTPConnection', Connection)
    client = transport.MemeBackendClient('http://example.test:80', 'x' * 32)
    client.forward('POST', '/api/v1/bots/ai/providers/test/00000000-0000-4000-8000-000000000001', b'', {})
    client.forward('GET', '/api/v1/bots/memes/runtime', b'', {})
    client.forward('POST', '/api/v1/bots/ai/drawing/check', b'', {}, timeout=120)
    assert timeouts == [65, 15, 120]
