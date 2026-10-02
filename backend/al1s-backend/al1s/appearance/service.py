"""Validated, shared login background stored as one platform S3 object."""

from io import BytesIO
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from al1s.kernel.types import BlobObjectHead

KEY = "platform/appearance/login-background"
MAX_BYTES = 8 * 1024 * 1024
TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImageStore(Protocol):
    def put(self, object_key: str, body: bytes, media_type: str) -> None: ...
    def head(self, object_key: str) -> BlobObjectHead: ...
    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes: ...


class AppearanceService:
    def __init__(self, store: ImageStore) -> None:
        self._store = store

    def upload(self, body: bytes, declared_type: str) -> str:
        if not body or len(body) > MAX_BYTES:
            raise ValueError("appearance_image_size")
        if declared_type not in TYPES.values():
            raise ValueError("appearance_image_type")
        try:
            with Image.open(BytesIO(body)) as image:
                actual = TYPES.get(image.format or "")
                width, height = image.size
                if width < 128 or height < 128 or width * height > 40_000_000:
                    raise ValueError("appearance_image_dimensions")
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ValueError("appearance_image_invalid") from exc
        if actual != declared_type:
            raise ValueError("appearance_image_type")
        self._store.put(KEY, body, declared_type)
        return declared_type

    def read(self) -> tuple[bytes, str]:
        head = self._store.head(KEY)
        if (
            head.size_bytes < 1
            or head.size_bytes > MAX_BYTES
            or head.media_type not in TYPES.values()
        ):
            raise ValueError("appearance_image_invalid")
        return self._store.get_range(KEY, 0, head.size_bytes - 1), head.media_type
