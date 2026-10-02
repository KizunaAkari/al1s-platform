from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import UUID, uuid4

from al1s.execution.artifact_upload_ports import (
    ArtifactObjectStore,
    ArtifactUploadUnitOfWork,
)
from al1s.execution.artifact_upload_types import (
    ArtifactKind,
    ArtifactOwnerKind,
    ArtifactStatus,
    ArtifactUploadRecord,
    PresignedArtifactUpload,
)
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.kernel.types import NewBlob

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_ARTIFACT_BYTES = 5 * 1024**3
UPLOAD_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class CreatedArtifactUpload:
    artifact: ArtifactUploadRecord
    upload: PresignedArtifactUpload | None


class ArtifactUploadService:
    def __init__(
        self,
        uow_factory: Callable[[], ArtifactUploadUnitOfWork],
        object_store: ArtifactObjectStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._object_store = object_store
        self._now = now or (lambda: datetime.now(UTC))
        self._completion_lock = Lock()
        self._completing: set[UUID] = set()

    def get(self, *, terminal_id: UUID, artifact_id: UUID) -> ArtifactUploadRecord:
        with self._uow_factory() as uow:
            artifact = uow.artifacts.get_for_terminal(artifact_id, terminal_id)
        if artifact is None:
            raise NotFoundError("artifact_upload")
        return artifact

    def is_completing(self, artifact_id: UUID) -> bool:
        with self._completion_lock:
            return artifact_id in self._completing

    def reserve_completion(self, artifact_id: UUID) -> bool:
        # Only a bounded I/O hint, not durable success. After process death the
        # terminal retries the same persisted artifact identity and keeps bytes.
        with self._completion_lock:
            if artifact_id in self._completing:
                return False
            if len(self._completing) >= 2:
                raise ConflictError("artifact_completion_busy", "Artifact verification is busy")
            self._completing.add(artifact_id)
            return True

    def release_completion(self, artifact_id: UUID) -> None:
        with self._completion_lock:
            self._completing.discard(artifact_id)

    def create(
        self,
        *,
        terminal_id: UUID,
        owner_kind: ArtifactOwnerKind,
        owner_id: UUID,
        artifact_kind: ArtifactKind,
        file_name: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        idempotency_key: str,
    ) -> CreatedArtifactUpload:
        self._validate(file_name, sha256, size_bytes, media_type, idempotency_key)
        now = self._now()
        artifact: ArtifactUploadRecord
        with self._uow_factory() as uow:
            uow.artifacts.acquire_idempotency_lock(terminal_id, idempotency_key)
            existing = uow.artifacts.find_by_idempotency(terminal_id, idempotency_key)
            if existing is not None:
                self._require_same_request(
                    existing,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    artifact_kind=artifact_kind,
                    file_name=file_name,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    media_type=media_type,
                )
                if existing.status is ArtifactStatus.PENDING and existing.expires_at <= now:
                    renewed = uow.artifacts.renew_pending(
                        existing.artifact_id,
                        existing.row_version,
                        now + UPLOAD_TTL,
                    )
                    if renewed is None:
                        raise ConflictError(
                            "artifact_upload_conflict",
                            "Artifact upload changed concurrently",
                        )
                    artifact = renewed
                    uow.commit()
                else:
                    artifact = existing
            else:
                if not uow.artifacts.owner_is_authorized(terminal_id, owner_kind, owner_id):
                    raise NotFoundError("artifact_owner")
                artifact_id = uuid4()
                artifact = ArtifactUploadRecord(
                    artifact_id=artifact_id,
                    terminal_id=terminal_id,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    artifact_kind=artifact_kind,
                    file_name=file_name,
                    expected_sha256=sha256,
                    expected_size_bytes=size_bytes,
                    media_type=media_type,
                    object_key=f"artifacts/{terminal_id}/{artifact_id}",
                    status=ArtifactStatus.PENDING,
                    idempotency_key=idempotency_key,
                    expires_at=now + UPLOAD_TTL,
                    completed_at=None,
                    blob_id=None,
                    created_at=now,
                    row_version=1,
                )
                uow.artifacts.add(artifact)
                uow.commit()
        return self._created(artifact, now)

    def complete(self, *, terminal_id: UUID, artifact_id: UUID) -> ArtifactUploadRecord:
        with self._uow_factory() as uow:
            artifact = uow.artifacts.get_for_terminal(artifact_id, terminal_id)
        if artifact is None:
            raise NotFoundError("artifact_upload")
        if artifact.status is ArtifactStatus.READY:
            return artifact
        now = self._now()
        if artifact.status is ArtifactStatus.EXPIRED or artifact.expires_at <= now:
            raise ConflictError("artifact_upload_expired", "Artifact upload session has expired")
        blob_id = uuid4()
        uploaded_blob = NewBlob(
            blob_id=blob_id,
            sha256=artifact.expected_sha256,
            size_bytes=artifact.expected_size_bytes,
            media_type=artifact.media_type,
            object_key=f"verified-artifacts/{blob_id}",
        )
        # Register before object I/O so interrupted finalizations remain traceable.
        with self._uow_factory() as uow:
            uow.blobs.add_pending(uploaded_blob)
            # Persist recovery before I/O: SIGKILL never executes finally.
            uow.gc_jobs.schedule(uuid4(), blob_id, now + timedelta(hours=24))
            uow.commit()
        try:
            self._object_store.finalize_artifact(
                artifact.object_key, uploaded_blob.object_key,
                size_bytes=artifact.expected_size_bytes,
                sha256=artifact.expected_sha256, media_type=artifact.media_type,
            )
            return self._publish(terminal_id, artifact_id, uploaded_blob)
        finally:
            # Version 1 can only be an unpublished candidate. Never delete on
            # ambiguous commit failure: a committed READY candidate is version 2.
            self._quarantine_candidate(uploaded_blob.blob_id)

    def _quarantine_candidate(self, blob_id: UUID) -> None:
        with self._uow_factory() as uow:
            if uow.blobs.mark_quarantined(blob_id, 1):
                uow.gc_jobs.schedule(uuid4(), blob_id, self._now() + timedelta(hours=1))
            uow.commit()

    def _publish(
        self, terminal_id: UUID, artifact_id: UUID, uploaded_blob: NewBlob,
    ) -> ArtifactUploadRecord:
        with self._uow_factory() as uow:
            current = uow.artifacts.get_for_terminal(artifact_id, terminal_id, for_update=True)
            if current is None:
                raise NotFoundError("artifact_upload")
            if current.status is ArtifactStatus.READY:
                return current
            uow.artifacts.acquire_blob_lock(current.expected_sha256)
            # Object I/O and both lock acquisitions may outlive the upload session.
            now = self._now()
            if current.status is not ArtifactStatus.PENDING or current.expires_at <= now:
                raise ConflictError(
                    "artifact_upload_expired", "Artifact upload session has expired"
                )
            ready_blob = uow.blobs.find_ready_by_sha256(current.expected_sha256)
            if ready_blob is not None and (
                ready_blob.size_bytes != current.expected_size_bytes
                or _base_media_type(ready_blob.media_type) != _base_media_type(current.media_type)
            ):
                raise ConflictError(
                    "artifact_blob_metadata_conflict",
                    "Existing Blob metadata differs from the artifact request",
                )
            if ready_blob is None:
                if not uow.blobs.mark_ready(uploaded_blob.blob_id, 1, now):
                    raise ConflictError("blob_state_conflict", "Artifact Blob changed")
                ready_blob = uow.blobs.find_ready_by_id(uploaded_blob.blob_id)
                assert ready_blob is not None
            else:
                if not uow.blobs.mark_quarantined(uploaded_blob.blob_id, 1):
                    raise ConflictError("blob_state_conflict", "Artifact Blob changed")
                uow.gc_jobs.schedule(uuid4(), uploaded_blob.blob_id, now)
            completed = uow.artifacts.mark_ready(
                current.artifact_id,
                current.row_version,
                ready_blob.blob_id,
                now,
            )
            if completed is None:
                raise ConflictError(
                    "artifact_upload_conflict", "Artifact upload changed concurrently"
                )
            uow.commit()
            return completed

    def _created(self, artifact: ArtifactUploadRecord, now: datetime) -> CreatedArtifactUpload:
        if artifact.status is ArtifactStatus.READY:
            return CreatedArtifactUpload(artifact, None)
        if artifact.status is not ArtifactStatus.PENDING or artifact.expires_at <= now:
            raise ConflictError("artifact_upload_expired", "Artifact upload session has expired")
        return CreatedArtifactUpload(
            artifact,
            self._object_store.create_presigned_upload(
                artifact.object_key,
                media_type=artifact.media_type,
                sha256=artifact.expected_sha256,
                expires_in=artifact.expires_at - now,
            ),
        )

    @staticmethod
    def _validate(
        file_name: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        idempotency_key: str,
    ) -> None:
        if not 1 <= len(file_name.strip()) <= 255 or "/" in file_name or "\\" in file_name:
            raise InvalidRequestError("invalid_artifact_name", "Artifact name is invalid")
        if SHA256_PATTERN.fullmatch(sha256) is None:
            raise InvalidRequestError("invalid_artifact_hash", "Artifact SHA-256 is invalid")
        if not 0 <= size_bytes <= MAX_ARTIFACT_BYTES:
            raise InvalidRequestError("invalid_artifact_size", "Artifact size is invalid")
        if not 1 <= len(media_type.strip()) <= 255:
            raise InvalidRequestError("invalid_artifact_media_type", "Media type is invalid")
        if not 1 <= len(idempotency_key.strip()) <= 128:
            raise InvalidRequestError("invalid_idempotency_key", "Idempotency key is invalid")

    @staticmethod
    def _require_same_request(
        artifact: ArtifactUploadRecord,
        *,
        owner_kind: ArtifactOwnerKind,
        owner_id: UUID,
        artifact_kind: ArtifactKind,
        file_name: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
    ) -> None:
        if (
            artifact.owner_kind is not owner_kind
            or artifact.owner_id != owner_id
            or artifact.artifact_kind is not artifact_kind
            or artifact.file_name != file_name
            or artifact.expected_sha256 != sha256
            or artifact.expected_size_bytes != size_bytes
            or _base_media_type(artifact.media_type) != _base_media_type(media_type)
        ):
            raise ConflictError(
                "artifact_idempotency_conflict",
                "Idempotency key was reused for a different artifact",
            )


def _base_media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()
