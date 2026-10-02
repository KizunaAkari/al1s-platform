"""Validated image staging using existing Blob persistence and GC."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4

from PIL import Image, ImageOps

from al1s.execution.errors import InvalidRequestError, NotFoundError
from al1s.kernel.ports import BlobStore
from al1s.kernel.types import NewBlob
from al1s.maa.image_resources import MAX_IMAGE_BYTES
from al1s.maa.ports import MaaUnitOfWork


def normalize_image(body: bytes) -> tuple[bytes, int, int]:
    if not 0 < len(body) <= MAX_IMAGE_BYTES:
        raise InvalidRequestError("lineup_image_size", "图片不得超过 16 MiB")
    try:
        with Image.open(BytesIO(body), formats=["PNG", "JPEG"]) as source:
            if source.width * source.height > 16_000_000 or max(source.size) > 8192:
                raise ValueError("dimensions")
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("frames")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.load()
        out = BytesIO()
        image.save(out, "PNG")
        if out.tell() > MAX_IMAGE_BYTES:
            raise ValueError("decoded size")
        return out.getvalue(), image.width, image.height
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InvalidRequestError(
            "lineup_image_invalid", "需要有效的 PNG/JPEG。最多 1600 万像素"
        ) from exc


class LineupImages:
    def __init__(self, uow: Callable[[], MaaUnitOfWork], store: BlobStore):
        self.uow, self.store = uow, store

    def stage(self, body: bytes) -> UUID:
        digest = hashlib.sha256(body).hexdigest()
        pending = NewBlob(uuid4(), digest, len(body), "image/png", f"lineup/{uuid4()}.png")
        with self.uow() as uow:
            old = uow.blobs.find_ready_by_sha256(digest)
            if old:
                return old.blob_id
            uow.blobs.add_pending(pending)
            uow.flush()
            uow.gc_jobs.schedule(uuid4(), pending.blob_id, datetime.now(UTC) + timedelta(hours=24))
            uow.commit()
        self.store.put(pending.object_key, body, "image/png")
        uploaded = self.store.get_range(pending.object_key, 0, len(body) - 1)
        if len(uploaded) != len(body) or hashlib.sha256(uploaded).hexdigest() != digest:
            raise InvalidRequestError("lineup_image_integrity", "图片存储校验失败。请重试")
        with self.uow() as uow:
            uow.imports.acquire_finalize_lock()
            old = uow.blobs.find_ready_by_sha256(digest)
            if old:
                return old.blob_id
            if not uow.blobs.mark_ready(pending.blob_id, 1, datetime.now(UTC)):
                raise InvalidRequestError("lineup_image_expired", "图片上传已过期。请重试")
            uow.commit()
        return pending.blob_id

    def read(self, blob_id: UUID) -> bytes:
        with self.uow() as uow:
            blob = uow.blobs.find_ready_by_id(blob_id)
            if blob is None:
                raise NotFoundError("lineup_image")
        if blob.media_type != "image/png" or not 0 < blob.size_bytes <= MAX_IMAGE_BYTES:
            raise InvalidRequestError("lineup_image_invalid", "原图格式无效")
        body = self.store.get_range(blob.object_key, 0, blob.size_bytes - 1)
        if len(body) != blob.size_bytes or hashlib.sha256(body).hexdigest() != blob.sha256:
            raise InvalidRequestError("lineup_image_integrity", "原图校验失败")
        return body
