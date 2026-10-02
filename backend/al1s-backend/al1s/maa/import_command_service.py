from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any
from uuid import UUID

from al1s.maa.archive import ParsedScriptArchive
from al1s.maa.archive_upload import validate_upload_size
from al1s.maa.errors import MaaDomainError
from al1s.maa.import_service import MaaArchiveImportService
from al1s.maa.mutation_service import (
    MaaMutationService,
    MutationExecution,
    MutationOutcome,
)
from al1s.maa.ports import BlobStore, MaaUnitOfWork
from al1s.maa.types import ImportBatchRecord, ImportResult

MaaUowFactory = Callable[[], MaaUnitOfWork]
MAX_CONTROLLED_ARCHIVE_BYTES = 512 * 1024 * 1024
ARCHIVE_MEDIA_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/x-zip-compressed",
        "application/zip",
    }
)


class MaaArchiveImportCommandService:
    """Import one catalogued archive without exposing server filesystem paths."""

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        blob_store: BlobStore,
        import_service: MaaArchiveImportService | None = None,
    ) -> None:
        self._blob_store = blob_store
        self._imports = import_service or MaaArchiveImportService(uow_factory, blob_store)
        self._mutations = MaaMutationService(uow_factory)

    def import_uploaded_archive(
        self, content: bytes, *, correlation_id: UUID, **selection: Any,
    ) -> ImportResult:
        validate_upload_size(content)
        # The importer deduplicates by verified logical hash. No outer transaction
        # is held while parsing or writing objects; a lost HTTP response is safe
        # to retry with the same archive.
        return self._imports.import_archive(content, correlation_id=correlation_id, **selection)

    def prepare_upload(
        self, content: bytes, **selection: Any,
    ) -> tuple[ParsedScriptArchive, ImportBatchRecord, bool]:
        validate_upload_size(content)
        return self._imports.prepare_archive(content, **selection)

    def finish_upload(
        self, parsed: ParsedScriptArchive, batch: ImportBatchRecord, *, correlation_id: UUID,
    ) -> ImportResult:
        return self._imports.finish_archive(parsed, batch, correlation_id=correlation_id)

    def cancel(self, batch_id: UUID, expected_version: int) -> ImportBatchRecord:
        return self._imports.cancel(batch_id, expected_version)

    def import_ready_blob(
        self,
        *,
        archive_blob_id: UUID,
        idempotency_key: str,
        correlation_id: UUID,
        target_applications: dict[str, UUID] | None = None,
        confirmed_overwrites: dict[UUID, int] | None = None,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation="maa.imports.create",
            idempotency_key=idempotency_key,
            request_payload={
                "archive_blob_id": str(archive_blob_id),
                "target_applications": {k: str(v) for k, v in (target_applications or {}).items()},
                "confirmed_overwrites": {
                    str(k): v for k, v in (confirmed_overwrites or {}).items()
                },
            },
            mutate=lambda uow: self._import(
                uow,
                archive_blob_id=archive_blob_id,
                correlation_id=correlation_id,
                target_applications=target_applications,
                confirmed_overwrites=confirmed_overwrites,
            ),
        )

    def _import(
        self,
        uow: MaaUnitOfWork,
        *,
        archive_blob_id: UUID,
        correlation_id: UUID,
        target_applications: dict[str, UUID] | None = None,
        confirmed_overwrites: dict[UUID, int] | None = None,
    ) -> MutationOutcome:
        blob = uow.blobs.find_ready_by_id(archive_blob_id)
        if blob is None:
            raise MaaDomainError(
                "archive_blob_not_ready",
                "Archive Blob does not exist or is not ready",
                409,
            )
        if blob.media_type not in ARCHIVE_MEDIA_TYPES:
            raise MaaDomainError(
                "invalid_archive_media_type",
                "Archive Blob must use a supported ZIP media type",
                422,
                context={"media_type": blob.media_type},
            )
        if not 0 < blob.size_bytes <= MAX_CONTROLLED_ARCHIVE_BYTES:
            raise MaaDomainError(
                "invalid_archive_blob_size",
                "Archive Blob size is outside the controlled import limit",
                422,
                context={
                    "size_bytes": blob.size_bytes,
                    "maximum_bytes": MAX_CONTROLLED_ARCHIVE_BYTES,
                },
            )

        try:
            content = self._blob_store.get_range(blob.object_key, 0, blob.size_bytes - 1)
        except KeyError as exc:
            raise MaaDomainError(
                "archive_blob_missing",
                "Archive Blob catalog entry has no object-store payload",
                409,
            ) from exc
        if len(content) != blob.size_bytes or hashlib.sha256(content).hexdigest() != blob.sha256:
            raise MaaDomainError(
                "archive_blob_integrity_mismatch",
                "Archive Blob payload does not match its catalog metadata",
                409,
            )

        imported = self._imports.import_archive(
            content, correlation_id=correlation_id, target_applications=target_applications,
            confirmed_overwrites=confirmed_overwrites,
        )
        return MutationOutcome(
            aggregate_type="maa_import_batch",
            aggregate_id=imported.batch.batch_id,
            response_status=200 if imported.replayed else 201,
            response_body=self._batch_body(imported.batch),
        )

    @staticmethod
    def _batch_body(batch: ImportBatchRecord) -> dict[str, Any]:
        return {
            "batch_id": str(batch.batch_id),
            "logical_sha256": batch.logical_sha256,
            "archive_sha256": batch.archive_sha256,
            "archive_schema": batch.archive_schema,
            "status": batch.status.value,
            "script_count": batch.script_count,
            "application_count": batch.application_count,
            "resource_reference_count": batch.resource_reference_count,
            "unique_resource_count": batch.unique_resource_count,
            "error_code": batch.error_code,
            "diagnostic": batch.diagnostic,
            "created_at": batch.created_at.isoformat(),
            "completed_at": (
                None if batch.completed_at is None else batch.completed_at.isoformat()
            ),
            "row_version": batch.row_version,
        }
