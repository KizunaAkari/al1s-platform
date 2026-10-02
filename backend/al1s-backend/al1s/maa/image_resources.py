from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4

from PIL import Image

from al1s.kernel.ports import BlobStore
from al1s.kernel.types import BlobRecord, NewBlob
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork

MAX_IMAGE_BYTES = 16 * 1024 * 1024


def validate_png(body: bytes) -> tuple[int, int]:
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise MaaDomainError("invalid_image_size", "PNG must be between 1 byte and 16 MiB", 422)
    try:
        with Image.open(BytesIO(body), formats=["PNG"]) as image:
            width, height = image.size
            if (
                max(width, height) > 8192
                or width * height > 16_777_216
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError("Image dimensions or frame count are not supported")
            image.verify()
        with Image.open(BytesIO(body), formats=["PNG"]) as image:
            image.load()  # Verify compressed pixels as well as PNG chunks.
        return width, height
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise MaaDomainError("invalid_png", "A valid single-frame PNG is required", 422) from exc


class MaaImageResourceService:
    """Bounded, content-addressed staging. No S3 calls while a DB transaction is open."""

    def __init__(self, uow_factory: Callable[[], MaaUnitOfWork], store: BlobStore) -> None:
        self._uow_factory, self._store = uow_factory, store

    def upload(self, script_id: UUID, body: bytes) -> dict[str, object]:
        width, height = validate_png(body)
        digest = hashlib.sha256(body).hexdigest()
        now = datetime.now(UTC)
        pending = NewBlob(uuid4(), digest, len(body), "image/png", f"maa/editor/{uuid4()}.png")
        with self._uow_factory() as uow:
            self._require_script(uow, script_id)
            existing = uow.blobs.find_ready_by_sha256(digest)
            if existing is not None:
                return self._response(existing, width, height)
            uow.blobs.add_pending(pending)
            uow.flush()
            if not uow.gc_jobs.schedule(uuid4(), pending.blob_id, now + timedelta(hours=24)):
                raise MaaDomainError(
                    "image_staging_failed", "Could not schedule staging cleanup", 409
                )
            uow.commit()
        self._store.put(pending.object_key, body, "image/png")
        head = self._store.head(pending.object_key)
        if head.size_bytes != len(body) or head.media_type.split(";", 1)[0] != "image/png":
            raise MaaDomainError(
                "image_upload_mismatch", "Uploaded PNG metadata does not match", 502
            )
        uploaded = self._store.get_range(pending.object_key, 0, len(body) - 1)
        if len(uploaded) != len(body) or hashlib.sha256(uploaded).hexdigest() != digest:
            raise MaaDomainError(
                "image_upload_mismatch", "Uploaded PNG checksum does not match", 502
            )
        with self._uow_factory() as uow:
            self._require_script(uow, script_id)
            # Same short finalize lock as imports prevents duplicate READY hashes.
            uow.imports.acquire_finalize_lock()
            existing = uow.blobs.find_ready_by_sha256(digest)
            if existing is None:
                if not uow.blobs.mark_ready(pending.blob_id, 1, datetime.now(UTC)):
                    raise MaaDomainError("image_staging_expired", "Upload reservation expired", 409)
                existing = uow.blobs.find_ready_by_id(pending.blob_id)
            if existing is None:
                raise MaaDomainError("image_staging_failed", "Uploaded image is not ready", 409)
            result = self._response(existing, width, height)
            uow.commit()
            return result

    @staticmethod
    def _require_script(uow: MaaUnitOfWork, script_id: UUID) -> None:
        if uow.scripts.get_active(script_id) is None:
            raise MaaDomainError("script_not_found", "Script was not found", 404)

    def read(self, script_id: UUID, version_id: UUID, blob_id: UUID) -> bytes:
        with self._uow_factory() as uow:
            self._require_script(uow, script_id)
            version = uow.script_versions.get(version_id)
            if (
                version is None
                or version.script_id != script_id
                or not uow.script_versions.has_blob_reference(version_id, blob_id)
            ):
                raise MaaDomainError("image_not_found", "Image is not in this script version", 404)
            blob = uow.blobs.find_ready_by_id(blob_id)
            if blob is None:
                raise MaaDomainError("image_not_found", "Image is not available", 404)
            if blob.media_type != "image/png" or not 0 < blob.size_bytes <= MAX_IMAGE_BYTES:
                raise MaaDomainError("unsupported_image", "Resource is not a bounded PNG", 422)
        body = self._store.get_range(blob.object_key, 0, blob.size_bytes - 1)
        if len(body) != blob.size_bytes or hashlib.sha256(body).hexdigest() != blob.sha256:
            raise MaaDomainError("image_checksum_mismatch", "Stored image checksum mismatch", 502)
        validate_png(body)
        return body

    @staticmethod
    def _response(blob: BlobRecord, width: int, height: int) -> dict[str, object]:
        if blob.media_type != "image/png":
            raise MaaDomainError(
                "image_metadata_conflict", "Resource media type is inconsistent", 409
            )
        return {
            "resource": {
                "$blob": str(blob.blob_id),
                "sha256": blob.sha256,
                "size_bytes": blob.size_bytes,
                "media_type": "image/png",
            },
            "width": width,
            "height": height,
        }
