from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from al1s.execution.blob_transfer_ports import TerminalBlobUnitOfWork
from al1s.execution.errors import InvalidRequestError, NotFoundError
from al1s.kernel.ports import BlobStore
from al1s.kernel.types import BlobRecord

MAX_BLOB_RANGE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TerminalBlobMetadata:
    blob_id: UUID
    sha256: str
    size_bytes: int
    media_type: str


class TerminalBlobTransferService:
    """Serve bounded S3 ranges only after database authorization."""

    def __init__(
        self,
        uow_factory: Callable[[], TerminalBlobUnitOfWork],
        blob_store: BlobStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store

    def metadata(self, terminal_id: UUID, blob_id: UUID) -> TerminalBlobMetadata:
        return _metadata(self._authorized_blob(terminal_id, blob_id))

    def _authorized_blob(self, terminal_id: UUID, blob_id: UUID) -> BlobRecord:
        with self._uow_factory() as uow:
            blob = uow.blob_access.find_authorized(terminal_id, blob_id)
        if blob is None:
            raise NotFoundError("terminal_blob")
        return blob

    def read_range(
        self,
        terminal_id: UUID,
        blob_id: UUID,
        *,
        start: int,
        end_inclusive: int,
    ) -> tuple[TerminalBlobMetadata, bytes]:
        blob = self._authorized_blob(terminal_id, blob_id)
        metadata = _metadata(blob)
        if start < 0 or end_inclusive < start or end_inclusive >= metadata.size_bytes:
            raise InvalidRequestError("invalid_blob_range", "Requested Blob range is invalid")
        requested_size = end_inclusive - start + 1
        if requested_size > MAX_BLOB_RANGE_BYTES:
            raise InvalidRequestError(
                "blob_range_too_large",
                f"Blob ranges must not exceed {MAX_BLOB_RANGE_BYTES} bytes",
            )
        body = self._blob_store.get_range(blob.object_key, start, end_inclusive)
        if len(body) != requested_size:
            raise RuntimeError("Blob store returned an incomplete byte range")
        return metadata, body


def _metadata(blob: BlobRecord) -> TerminalBlobMetadata:
    return TerminalBlobMetadata(
        blob_id=blob.blob_id,
        sha256=blob.sha256,
        size_bytes=blob.size_bytes,
        media_type=blob.media_type,
    )
