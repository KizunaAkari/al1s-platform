from __future__ import annotations

from datetime import datetime, timedelta
from types import TracebackType
from typing import Protocol
from uuid import UUID

from al1s.execution.artifact_upload_types import (
    ArtifactObjectHead,
    ArtifactOwnerKind,
    ArtifactUploadRecord,
    PresignedArtifactUpload,
)
from al1s.kernel.ports import BlobCatalogRepository, GcJobRepository


class ArtifactUploadRepository(Protocol):
    def acquire_idempotency_lock(self, terminal_id: UUID, idempotency_key: str) -> None: ...

    def acquire_blob_lock(self, sha256: str) -> None: ...

    def find_by_idempotency(
        self, terminal_id: UUID, idempotency_key: str
    ) -> ArtifactUploadRecord | None: ...

    def get_for_terminal(
        self, artifact_id: UUID, terminal_id: UUID, *, for_update: bool = False
    ) -> ArtifactUploadRecord | None: ...

    def owner_is_authorized(
        self, terminal_id: UUID, owner_kind: ArtifactOwnerKind, owner_id: UUID
    ) -> bool: ...

    def add(self, artifact: ArtifactUploadRecord) -> None: ...

    def renew_pending(
        self,
        artifact_id: UUID,
        expected_version: int,
        expires_at: datetime,
    ) -> ArtifactUploadRecord | None: ...

    def mark_ready(
        self,
        artifact_id: UUID,
        expected_version: int,
        blob_id: UUID,
        completed_at: datetime,
    ) -> ArtifactUploadRecord | None: ...


class ArtifactUploadUnitOfWork(Protocol):
    artifacts: ArtifactUploadRepository
    blobs: BlobCatalogRepository
    gc_jobs: GcJobRepository

    def __enter__(self) -> ArtifactUploadUnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...


class ArtifactObjectStore(Protocol):
    def finalize_artifact(
        self, source_key: str, destination_key: str, *,
        size_bytes: int, sha256: str, media_type: str,
    ) -> None: ...

    def create_presigned_upload(
        self,
        object_key: str,
        *,
        media_type: str,
        sha256: str,
        expires_in: timedelta,
    ) -> PresignedArtifactUpload: ...

    def head_artifact(self, object_key: str) -> ArtifactObjectHead: ...

    def delete(self, object_key: str) -> None: ...
