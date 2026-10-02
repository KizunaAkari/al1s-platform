from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from al1s.execution.artifact_upload_service import ArtifactUploadService
from al1s.execution.artifact_upload_types import (
    ArtifactKind,
    ArtifactObjectHead,
    ArtifactOwnerKind,
    ArtifactStatus,
    ArtifactUploadRecord,
    PresignedArtifactUpload,
)
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.kernel.types import BlobRecord, BlobStatus, NewBlob

NOW = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


class FakeArtifactRepository:
    def __init__(self) -> None:
        self.authorized = True
        self.records: dict[UUID, ArtifactUploadRecord] = {}

    def acquire_idempotency_lock(self, terminal_id: UUID, idempotency_key: str) -> None:
        return

    def acquire_blob_lock(self, sha256: str) -> None:
        return

    def find_by_idempotency(
        self, terminal_id: UUID, idempotency_key: str
    ) -> ArtifactUploadRecord | None:
        return next(
            (
                item
                for item in self.records.values()
                if item.terminal_id == terminal_id and item.idempotency_key == idempotency_key
            ),
            None,
        )

    def get_for_terminal(
        self, artifact_id: UUID, terminal_id: UUID, *, for_update: bool = False
    ) -> ArtifactUploadRecord | None:
        item = self.records.get(artifact_id)
        return item if item is not None and item.terminal_id == terminal_id else None

    def owner_is_authorized(
        self, terminal_id: UUID, owner_kind: ArtifactOwnerKind, owner_id: UUID
    ) -> bool:
        return self.authorized

    def add(self, artifact: ArtifactUploadRecord) -> None:
        self.records[artifact.artifact_id] = artifact

    def renew_pending(
        self,
        artifact_id: UUID,
        expected_version: int,
        expires_at: datetime,
    ) -> ArtifactUploadRecord | None:
        current = self.records[artifact_id]
        if current.row_version != expected_version or current.status is not ArtifactStatus.PENDING:
            return None
        renewed = replace(
            current,
            expires_at=expires_at,
            row_version=current.row_version + 1,
        )
        self.records[artifact_id] = renewed
        return renewed

    def mark_ready(
        self,
        artifact_id: UUID,
        expected_version: int,
        blob_id: UUID,
        completed_at: datetime,
    ) -> ArtifactUploadRecord | None:
        current = self.records[artifact_id]
        if current.row_version != expected_version or current.status is not ArtifactStatus.PENDING:
            return None
        ready = ArtifactUploadRecord(
            artifact_id=current.artifact_id,
            terminal_id=current.terminal_id,
            owner_kind=current.owner_kind,
            owner_id=current.owner_id,
            artifact_kind=current.artifact_kind,
            file_name=current.file_name,
            expected_sha256=current.expected_sha256,
            expected_size_bytes=current.expected_size_bytes,
            media_type=current.media_type,
            object_key=current.object_key,
            status=ArtifactStatus.READY,
            idempotency_key=current.idempotency_key,
            expires_at=current.expires_at,
            completed_at=completed_at,
            blob_id=blob_id,
            created_at=current.created_at,
            row_version=current.row_version + 1,
        )
        self.records[artifact_id] = ready
        return ready


class FakeBlobRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, BlobRecord] = {}

    def add_pending(self, blob: NewBlob) -> None:
        self.records[blob.blob_id] = BlobRecord(
            blob_id=blob.blob_id,
            sha256=blob.sha256,
            size_bytes=blob.size_bytes,
            media_type=blob.media_type,
            object_key=blob.object_key,
            status=BlobStatus.PENDING,
            row_version=1,
            created_at=NOW,
            ready_at=None,
            deleted_at=None,
        )

    def find_ready_by_sha256(self, sha256: str) -> BlobRecord | None:
        return next(
            (
                item
                for item in self.records.values()
                if item.sha256 == sha256 and item.status is BlobStatus.READY
            ),
            None,
        )

    def find_ready_by_id(self, blob_id: UUID) -> BlobRecord | None:
        item = self.records.get(blob_id)
        return item if item is not None and item.status is BlobStatus.READY else None

    def mark_ready(self, blob_id: UUID, expected_version: int, ready_at: datetime) -> bool:
        current = self.records[blob_id]
        self.records[blob_id] = _blob_with_status(current, BlobStatus.READY, ready_at)
        return True

    def mark_quarantined(self, blob_id: UUID, expected_version: int) -> bool:
        current = self.records[blob_id]
        if current.row_version != expected_version or current.status is not BlobStatus.PENDING:
            return False
        self.records[blob_id] = _blob_with_status(current, BlobStatus.QUARANTINED, None)
        return True


class FakeGcRepository:
    def __init__(self) -> None:
        self.scheduled = []

    def schedule(self, job_id: UUID, blob_id: UUID, available_at: datetime) -> bool:
        self.scheduled.append((blob_id, available_at))
        return True


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.artifacts = FakeArtifactRepository()
        self.blobs = FakeBlobRepository()
        self.gc_jobs = FakeGcRepository()

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        return

    def commit(self) -> None:
        return


class FakeObjectStore:
    def __init__(self) -> None:
        self.head = ArtifactObjectHead(4, "image/png", "a" * 64)

    def create_presigned_upload(
        self,
        object_key: str,
        *,
        media_type: str,
        sha256: str,
        expires_in: timedelta,
    ) -> PresignedArtifactUpload:
        return PresignedArtifactUpload(
            "PUT",
            f"https://objects.test/{object_key}",
            {"Content-Type": media_type, "x-amz-meta-sha256": sha256},
            NOW + expires_in,
        )

    def head_artifact(self, object_key: str) -> ArtifactObjectHead:
        return self.head

    def finalize_artifact(
        self,
        source_key: str,
        destination_key: str,
        *,
        size_bytes: int,
        sha256: str,
        media_type: str,
    ) -> None:
        assert source_key != destination_key
        head = self.head_artifact(source_key)
        if head != ArtifactObjectHead(size_bytes, media_type, sha256):
            raise ConflictError("artifact_upload_mismatch", "metadata mismatch")

    def delete(self, object_key: str) -> None:
        return


def _service() -> tuple[ArtifactUploadService, FakeUnitOfWork, FakeObjectStore]:
    uow = FakeUnitOfWork()
    store = FakeObjectStore()
    return (
        ArtifactUploadService(lambda: uow, store, now=lambda: NOW),  # type: ignore[arg-type]
        uow,
        store,
    )


def _create(service: ArtifactUploadService, terminal_id: UUID, owner_id: UUID):
    return service.create(
        terminal_id=terminal_id,
        owner_kind=ArtifactOwnerKind.FORMAL_ATTEMPT,
        owner_id=owner_id,
        artifact_kind=ArtifactKind.SCREENSHOT,
        file_name="failure.png",
        sha256="a" * 64,
        size_bytes=4,
        media_type="image/png",
        idempotency_key="artifact-1",
    )


def test_async_response_precedes_storage_io_and_duplicate_does_not_schedule_twice(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from fastapi import BackgroundTasks, Response

    from al1s.api import terminal_artifacts as api

    service, _, _ = _service()
    terminal_id = uuid4()
    artifact = _create(service, terminal_id, uuid4()).artifact
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifact_uploads=service)))
    monkeypatch.setattr(api, "authenticated_terminal_id", lambda *args: terminal_id)
    calls = []
    monkeypatch.setattr(service, "complete", lambda **kwargs: calls.append(kwargs))
    background, response = BackgroundTasks(), Response()
    result = api.complete_artifact_upload(
        artifact.artifact_id,
        request,
        background,
        response,
        authorization="Bearer test",
        prefer="respond-async",
    )
    assert response.status_code == 202 and result.processing and not calls
    duplicate = BackgroundTasks()
    api.complete_artifact_upload(
        artifact.artifact_id,
        request,
        duplicate,
        Response(),
        authorization="Bearer test",
        prefer="respond-async",
    )
    assert not duplicate.tasks
    asyncio.run(background())
    assert len(calls) == 1 and not service.is_completing(artifact.artifact_id)


def test_completion_slots_are_bounded_and_get_is_owner_scoped() -> None:
    service, _, _ = _service()
    terminal_id = uuid4()
    created = _create(service, terminal_id, uuid4())
    artifact_id = created.artifact.artifact_id
    assert service.get(terminal_id=terminal_id, artifact_id=artifact_id) == created.artifact
    with pytest.raises(NotFoundError):
        service.get(terminal_id=uuid4(), artifact_id=artifact_id)
    assert service.reserve_completion(artifact_id)
    assert not service.reserve_completion(artifact_id)
    assert service.reserve_completion(uuid4())
    with pytest.raises(ConflictError):
        service.reserve_completion(uuid4())
    service.release_completion(artifact_id)
    assert not service.is_completing(artifact_id)
    assert service.reserve_completion(uuid4())


def test_background_completion_releases_slot_on_failure(monkeypatch) -> None:
    from al1s.api.terminal_artifacts import _finish_completion

    service, _, _ = _service()
    artifact_id = uuid4()
    assert service.reserve_completion(artifact_id)

    def fail(**kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(service, "complete", fail)
    _finish_completion(service, uuid4(), artifact_id)
    assert not service.is_completing(artifact_id)


def test_upload_session_is_idempotent_and_completes_to_ready_blob() -> None:
    service, uow, _store = _service()
    terminal_id = uuid4()
    owner_id = uuid4()

    created = _create(service, terminal_id, owner_id)
    replay = _create(service, terminal_id, owner_id)
    completed = service.complete(terminal_id=terminal_id, artifact_id=created.artifact.artifact_id)

    assert created.artifact.status is ArtifactStatus.PENDING
    assert created.upload is not None
    assert replay.artifact.artifact_id == created.artifact.artifact_id
    assert completed.status is ArtifactStatus.READY
    assert completed.blob_id is not None
    blob = uow.blobs.records[completed.blob_id]
    assert blob.status is BlobStatus.READY
    assert blob.object_key != created.artifact.object_key
    assert service.complete(terminal_id=terminal_id, artifact_id=completed.artifact_id) == completed
    assert len(uow.blobs.records) == 1


def test_candidate_cleanup_exists_before_storage_io(monkeypatch) -> None:
    service, uow, store = _service()
    terminal = uuid4()
    created = _create(service, terminal, uuid4())

    def interrupted(*args, **kwargs):
        assert len(uow.gc_jobs.scheduled) == 1
        blob_id, deadline = uow.gc_jobs.scheduled[0]
        assert blob_id in uow.blobs.records
        assert deadline == NOW + timedelta(hours=24)
        raise RuntimeError("storage interrupted")

    monkeypatch.setattr(store, "finalize_artifact", interrupted)
    with pytest.raises(RuntimeError, match="storage interrupted"):
        service.complete(terminal_id=terminal, artifact_id=created.artifact.artifact_id)


def test_known_hash_still_requires_upload_then_deduplicates() -> None:
    service, uow, _store = _service()
    terminal = uuid4()
    first = _create(service, terminal, uuid4())
    ready = service.complete(terminal_id=terminal, artifact_id=first.artifact.artifact_id)
    second_terminal = uuid4()
    second = _create(service, second_terminal, uuid4())
    assert second.upload is not None
    assert second.artifact.status is ArtifactStatus.PENDING
    completed = service.complete(
        terminal_id=second_terminal,
        artifact_id=second.artifact.artifact_id,
    )
    assert completed.blob_id == ready.blob_id
    assert sorted(b.status.value for b in uow.blobs.records.values()) == ["quarantined", "ready"]


def test_expired_pending_upload_is_renewed_with_same_artifact_identity() -> None:
    service, uow, _store = _service()
    terminal_id = uuid4()
    owner_id = uuid4()
    created = _create(service, terminal_id, owner_id)
    uow.artifacts.records[created.artifact.artifact_id] = replace(
        created.artifact,
        expires_at=NOW - timedelta(seconds=1),
    )

    renewed = _create(service, terminal_id, owner_id)

    assert renewed.artifact.artifact_id == created.artifact.artifact_id
    assert renewed.artifact.expires_at == NOW + timedelta(minutes=15)
    assert renewed.artifact.row_version == created.artifact.row_version + 1
    assert renewed.upload is not None


def test_upload_rejects_unauthorized_owner_and_mismatched_object() -> None:
    service, uow, store = _service()
    uow.artifacts.authorized = False
    with pytest.raises(NotFoundError):
        _create(service, uuid4(), uuid4())

    uow.artifacts.authorized = True
    terminal_id = uuid4()
    created = _create(service, terminal_id, uuid4())
    store.head = ArtifactObjectHead(3, "image/png", "a" * 64)
    with pytest.raises(ConflictError, match="metadata"):
        service.complete(terminal_id=terminal_id, artifact_id=created.artifact.artifact_id)


@pytest.mark.parametrize("overrun", [timedelta(0), timedelta(seconds=1)])
def test_upload_expiring_during_object_check_cannot_be_completed(overrun: timedelta) -> None:
    uow = FakeUnitOfWork()
    clock = [NOW]

    class SlowObjectStore(FakeObjectStore):
        def head_artifact(self, object_key: str) -> ArtifactObjectHead:
            clock[0] = NOW + timedelta(minutes=15) + overrun
            return super().head_artifact(object_key)

    service = ArtifactUploadService(
        lambda: uow,
        SlowObjectStore(),
        now=lambda: clock[0],  # type: ignore[arg-type]
    )
    terminal_id = uuid4()
    created = _create(service, terminal_id, uuid4())

    with pytest.raises(ConflictError, match="expired"):
        service.complete(terminal_id=terminal_id, artifact_id=created.artifact.artifact_id)

    assert uow.artifacts.records[created.artifact.artifact_id].status is ArtifactStatus.PENDING
    assert all(blob.status is BlobStatus.QUARANTINED for blob in uow.blobs.records.values())


def _blob_with_status(
    blob: BlobRecord, status: BlobStatus, ready_at: datetime | None
) -> BlobRecord:
    return BlobRecord(
        blob_id=blob.blob_id,
        sha256=blob.sha256,
        size_bytes=blob.size_bytes,
        media_type=blob.media_type,
        object_key=blob.object_key,
        status=status,
        row_version=blob.row_version + 1,
        created_at=blob.created_at,
        ready_at=ready_at,
        deleted_at=None,
    )


def test_expiry_is_rechecked_after_waiting_for_hash_lock() -> None:
    uow = FakeUnitOfWork()
    clock = [NOW]

    def acquire_hash_lock(sha256: str) -> None:
        clock[0] = NOW + timedelta(minutes=15)

    uow.artifacts.acquire_blob_lock = acquire_hash_lock  # type: ignore[method-assign]
    service = ArtifactUploadService(
        lambda: uow,
        FakeObjectStore(),
        now=lambda: clock[0],  # type: ignore[arg-type]
    )
    terminal = uuid4()
    created = _create(service, terminal, uuid4())
    with pytest.raises(ConflictError, match="expired"):
        service.complete(terminal_id=terminal, artifact_id=created.artifact.artifact_id)
    assert all(b.status is BlobStatus.QUARANTINED for b in uow.blobs.records.values())


def test_overlapping_completions_only_publish_one_candidate() -> None:
    service, uow, store = _service()
    terminal = uuid4()
    created = _create(service, terminal, uuid4())
    original = store.finalize_artifact
    nested = []

    def finalize(*args, **kwargs):
        original(*args, **kwargs)
        if not nested:
            nested.append(True)
            service.complete(terminal_id=terminal, artifact_id=created.artifact.artifact_id)

    store.finalize_artifact = finalize  # type: ignore[method-assign]
    completed = service.complete(terminal_id=terminal, artifact_id=created.artifact.artifact_id)
    assert uow.blobs.records[completed.blob_id].status is BlobStatus.READY
    assert sorted(b.status.value for b in uow.blobs.records.values()) == ["quarantined", "ready"]


def test_ambiguous_publish_commit_does_not_quarantine_ready_object() -> None:
    service, uow, _store = _service()
    terminal = uuid4()
    created = _create(service, terminal, uuid4())

    def commit():
        if uow.artifacts.records[created.artifact.artifact_id].status is ArtifactStatus.READY:
            raise OSError("commit acknowledgement lost")

    uow.commit = commit  # type: ignore[method-assign]
    with pytest.raises(OSError):
        service.complete(terminal_id=terminal, artifact_id=created.artifact.artifact_id)
    assert all(b.status is BlobStatus.READY for b in uow.blobs.records.values())
    # A retry observes the durable completion and does not create another object.
    completed = service.complete(terminal_id=terminal, artifact_id=created.artifact.artifact_id)
    assert completed.status is ArtifactStatus.READY
    assert len(uow.blobs.records) == 1
