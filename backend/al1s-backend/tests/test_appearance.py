import asyncio
from io import BytesIO
from threading import Event

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from starlette.requests import Request

from al1s.adapters.s3.blob_store import BlobNotFoundError
from al1s.api.appearance import router, upload
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.appearance.service import KEY, AppearanceService
from al1s.kernel.types import BlobObjectHead


class MemoryImageStore:
    def __init__(self) -> None:
        self.body: bytes | None = None
        self.media_type = ""

    def put(self, object_key: str, body: bytes, media_type: str) -> None:
        assert object_key == KEY
        self.body, self.media_type = body, media_type

    def head(self, object_key: str) -> BlobObjectHead:
        assert object_key == KEY
        if self.body is None:
            raise BlobNotFoundError(object_key)
        return BlobObjectHead(len(self.body), self.media_type, None)

    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes:
        assert object_key == KEY and self.body is not None
        return self.body[start : end_inclusive + 1]


def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (128, 128), "blue").save(output, format="PNG")
    return output.getvalue()


def test_background_is_public_but_upload_requires_admin_and_valid_image() -> None:
    app = FastAPI()
    store = MemoryImageStore()
    app.state.appearance = AppearanceService(store)
    app.state.settings = Settings(
        admin_password="appearance-test-password", admin_cookie_secure=False
    )
    install_admin_auth(app, app.state.settings)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        assert client.get("/api/v1/appearance/login-background").status_code == 404
        unauth = client.put("/api/v1/appearance/login-background", content=png(),
                            headers={"Content-Type": "image/png", "X-AL1S-CSRF": "1"})
        assert unauth.status_code == 401
        assert client.post("/api/v1/auth/login", json={"password": "appearance-test-password"},
                           headers={"X-AL1S-CSRF": "1"}).status_code == 200
        bad = client.put("/api/v1/appearance/login-background", content=b"not an image",
                         headers={"Content-Type": "image/png", "X-AL1S-CSRF": "1"})
        assert bad.status_code == 422
        saved = client.put("/api/v1/appearance/login-background", content=png(),
                           headers={"Content-Type": "image/png", "X-AL1S-CSRF": "1"})
        assert saved.status_code == 200
        assert saved.json() == {"media_type": "image/png"}
        client.post("/api/v1/auth/logout", headers={"X-AL1S-CSRF": "1"})
        public = client.get("/api/v1/appearance/login-background")
        assert public.status_code == 200
        assert public.headers["content-type"] == "image/png"
        assert public.content == png()


def test_slow_background_store_does_not_block_async_requests() -> None:
    class SlowStore(MemoryImageStore):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Event()
            self.release = Event()

        def put(self, object_key: str, body: bytes, media_type: str) -> None:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("store did not resume")
            super().put(object_key, body, media_type)

    app = FastAPI()
    store = SlowStore()
    app.state.appearance = AppearanceService(store)
    body = png()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "method": "PUT", "path": "/api/v1/appearance/login-background",
        "headers": [(b"content-type", b"image/png")], "app": app,
    }, receive)

    async def check() -> None:
        task = asyncio.create_task(upload(request))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(store.entered.wait, 2), 2)
            assert not task.done()
            await asyncio.wait_for(asyncio.sleep(0), 0.5)
        finally:
            store.release.set()
        assert await task == {"media_type": "image/png"}
        assert store.body == body

    asyncio.run(check())
