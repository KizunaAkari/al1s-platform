from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.models import BlobObjectRow
from al1s.adapters.postgres.scheduling_models import (
    ExecutionSnapshotBlobRow,
    ExecutionSnapshotRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    _snapshot_record,
)
from al1s.execution.delivery_types import PackageResource
from al1s.execution.scheduling_types import (
    ExecutionSnapshotRecord,
    SnapshotBlobReference,
)
from al1s.kernel.types import BlobStatus


class PostgresSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def ready_blob_ids(self, blob_ids: set[UUID]) -> set[UUID]:
        if not blob_ids:
            return set()
        return set(
            self._session.scalars(
                select(BlobObjectRow.id).where(
                    BlobObjectRow.id.in_(blob_ids),
                    BlobObjectRow.status == BlobStatus.READY.value,
                )
            ).all()
        )

    def list_package_resources(self, snapshot_id: UUID) -> tuple[PackageResource, ...]:
        rows = self._session.execute(
            select(
                ExecutionSnapshotBlobRow.blob_id,
                ExecutionSnapshotBlobRow.resource_key,
                ExecutionSnapshotBlobRow.role,
                BlobObjectRow.sha256,
                BlobObjectRow.size_bytes,
                BlobObjectRow.media_type,
            )
            .join(BlobObjectRow, BlobObjectRow.id == ExecutionSnapshotBlobRow.blob_id)
            .where(
                ExecutionSnapshotBlobRow.snapshot_id == snapshot_id,
                BlobObjectRow.status == BlobStatus.READY.value,
            )
            .order_by(ExecutionSnapshotBlobRow.resource_key)
        ).all()
        return tuple(
            PackageResource(
                blob_id=row.blob_id,
                resource_key=row.resource_key,
                role=row.role,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                media_type=row.media_type,
            )
            for row in rows
        )

    def add(
        self,
        snapshot: ExecutionSnapshotRecord,
        blobs: Sequence[SnapshotBlobReference],
    ) -> None:
        self._session.add(
            ExecutionSnapshotRow(
                id=snapshot.snapshot_id,
                execution_id=snapshot.execution_id,
                source_module=snapshot.source_module,
                logical_content_id=snapshot.logical_content_id,
                revision_id=snapshot.revision_id,
                schema_version=snapshot.schema_version,
                manifest_hash=snapshot.manifest_hash,
                manifest=dict(snapshot.manifest),
                parameters=dict(snapshot.parameters),
                capability_requirements={
                    "schema_version": snapshot.capability_requirements.schema_version,
                    "min_protocol_version": (snapshot.capability_requirements.min_protocol_version),
                    "architectures": list(snapshot.capability_requirements.architectures),
                    "min_memory_bytes": snapshot.capability_requirements.min_memory_bytes,
                    "min_storage_bytes": snapshot.capability_requirements.min_storage_bytes,
                    "accelerator_type": snapshot.capability_requirements.accelerator_type,
                    "provider_keys": list(snapshot.capability_requirements.provider_keys),
                    "requires_target_device": (
                        snapshot.capability_requirements.requires_target_device
                    ),
                },
                terminal_id=snapshot.terminal_id,
                target_device_id=snapshot.target_device_id,
                timeout_seconds=snapshot.timeout_seconds,
                max_retries=snapshot.max_retries,
                record_video=snapshot.record_video,
                created_at=snapshot.created_at,
            )
        )
        # Snapshot blobs are immutable children of the snapshot row.  This
        # repository owns their persistence ordering, while the surrounding
        # unit of work still owns the transaction and commit.
        self._session.flush()
        self._session.add_all(
            [
                ExecutionSnapshotBlobRow(
                    snapshot_id=snapshot.snapshot_id,
                    resource_key=blob.resource_key,
                    blob_id=blob.blob_id,
                    role=blob.role,
                )
                for blob in blobs
            ]
        )

    def get_by_execution(self, execution_id: UUID) -> ExecutionSnapshotRecord | None:
        row = self._session.scalar(
            select(ExecutionSnapshotRow).where(ExecutionSnapshotRow.execution_id == execution_id)
        )
        return _snapshot_record(row) if row else None

    def list_blobs(self, snapshot_id: UUID) -> tuple[SnapshotBlobReference, ...]:
        rows = self._session.scalars(
            select(ExecutionSnapshotBlobRow)
            .where(ExecutionSnapshotBlobRow.snapshot_id == snapshot_id)
            .order_by(ExecutionSnapshotBlobRow.resource_key)
        ).all()
        return tuple(SnapshotBlobReference(row.blob_id, row.resource_key, row.role) for row in rows)
