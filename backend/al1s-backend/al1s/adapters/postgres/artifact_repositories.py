from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow
from al1s.adapters.postgres.delivery_models import TaskPackageRow
from al1s.adapters.postgres.maa_models import MaaQuickTestSessionRow
from al1s.execution.artifact_upload_types import (
    ArtifactKind,
    ArtifactOwnerKind,
    ArtifactStatus,
    ArtifactUploadRecord,
)
from al1s.execution.delivery_types import PackageStatus
from al1s.maa.types import QuickTestSessionStatus


class PostgresArtifactUploadRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire_idempotency_lock(self, terminal_id: UUID, idempotency_key: str) -> None:
        lock_key = f"artifact:{terminal_id}:{idempotency_key}"
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": lock_key},
        )

    def acquire_blob_lock(self, sha256: str) -> None:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"artifact-blob:{sha256}"},
        )

    def find_by_idempotency(
        self, terminal_id: UUID, idempotency_key: str
    ) -> ArtifactUploadRecord | None:
        row = self._session.scalar(
            select(TerminalArtifactRow).where(
                TerminalArtifactRow.terminal_id == terminal_id,
                TerminalArtifactRow.idempotency_key == idempotency_key,
            )
        )
        return None if row is None else _record(row)

    def get_for_terminal(
        self, artifact_id: UUID, terminal_id: UUID, *, for_update: bool = False
    ) -> ArtifactUploadRecord | None:
        statement = select(TerminalArtifactRow).where(
            TerminalArtifactRow.id == artifact_id,
            TerminalArtifactRow.terminal_id == terminal_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return None if row is None else _record(row)

    def owner_is_authorized(
        self, terminal_id: UUID, owner_kind: ArtifactOwnerKind, owner_id: UUID
    ) -> bool:
        if owner_kind is ArtifactOwnerKind.FORMAL_ATTEMPT:
            statement = select(
                exists().where(
                    TaskPackageRow.attempt_id == owner_id,
                    TaskPackageRow.terminal_id == terminal_id,
                    TaskPackageRow.status.in_(
                        (PackageStatus.AVAILABLE.value, PackageStatus.ACCEPTED.value)
                    ),
                )
            )
        else:
            statement = select(
                exists().where(
                    MaaQuickTestSessionRow.id == owner_id,
                    MaaQuickTestSessionRow.terminal_id == terminal_id,
                    MaaQuickTestSessionRow.status.in_(
                        (
                            QuickTestSessionStatus.CLAIMED.value,
                            QuickTestSessionStatus.COMPLETED.value,
                        )
                    ),
                )
            )
        return bool(self._session.scalar(statement))

    def add(self, artifact: ArtifactUploadRecord) -> None:
        self._session.add(
            TerminalArtifactRow(
                id=artifact.artifact_id,
                terminal_id=artifact.terminal_id,
                owner_kind=artifact.owner_kind.value,
                owner_id=artifact.owner_id,
                artifact_kind=artifact.artifact_kind.value,
                file_name=artifact.file_name,
                expected_sha256=artifact.expected_sha256,
                expected_size_bytes=artifact.expected_size_bytes,
                media_type=artifact.media_type,
                object_key=artifact.object_key,
                status=artifact.status.value,
                idempotency_key=artifact.idempotency_key,
                expires_at=artifact.expires_at,
                completed_at=artifact.completed_at,
                blob_id=artifact.blob_id,
                created_at=artifact.created_at,
                row_version=artifact.row_version,
            )
        )

    def renew_pending(
        self,
        artifact_id: UUID,
        expected_version: int,
        expires_at: datetime,
    ) -> ArtifactUploadRecord | None:
        row = self._session.scalar(
            update(TerminalArtifactRow)
            .where(
                TerminalArtifactRow.id == artifact_id,
                TerminalArtifactRow.status == ArtifactStatus.PENDING.value,
                TerminalArtifactRow.row_version == expected_version,
            )
            .values(
                expires_at=expires_at,
                row_version=TerminalArtifactRow.row_version + 1,
            )
            .returning(TerminalArtifactRow)
        )
        return None if row is None else _record(row)

    def mark_ready(
        self,
        artifact_id: UUID,
        expected_version: int,
        blob_id: UUID,
        completed_at: datetime,
    ) -> ArtifactUploadRecord | None:
        row = self._session.scalar(
            update(TerminalArtifactRow)
            .where(
                TerminalArtifactRow.id == artifact_id,
                TerminalArtifactRow.status == ArtifactStatus.PENDING.value,
                TerminalArtifactRow.row_version == expected_version,
            )
            .values(
                status=ArtifactStatus.READY.value,
                blob_id=blob_id,
                completed_at=completed_at,
                staging_cleanup_at=func.greatest(
                    TerminalArtifactRow.expires_at, completed_at
                ) + timedelta(hours=2),
                row_version=TerminalArtifactRow.row_version + 1,
            )
            .returning(TerminalArtifactRow)
        )
        return None if row is None else _record(row)


def _record(row: TerminalArtifactRow) -> ArtifactUploadRecord:
    return ArtifactUploadRecord(
        artifact_id=row.id,
        terminal_id=row.terminal_id,
        owner_kind=ArtifactOwnerKind(row.owner_kind),
        owner_id=row.owner_id,
        artifact_kind=ArtifactKind(row.artifact_kind),
        file_name=row.file_name,
        expected_sha256=row.expected_sha256,
        expected_size_bytes=row.expected_size_bytes,
        media_type=row.media_type,
        object_key=row.object_key,
        status=ArtifactStatus(row.status),
        idempotency_key=row.idempotency_key,
        expires_at=row.expires_at,
        completed_at=row.completed_at,
        blob_id=row.blob_id,
        created_at=row.created_at,
        row_version=row.row_version,
    )
