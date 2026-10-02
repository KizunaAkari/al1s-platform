from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import TaskPackageRow
from al1s.adapters.postgres.maa_models import MaaQuickTestBlobRow, MaaQuickTestSessionRow
from al1s.adapters.postgres.models import BlobObjectRow
from al1s.adapters.postgres.scheduling_models import ExecutionSnapshotBlobRow
from al1s.execution.delivery_types import PackageStatus
from al1s.kernel.types import BlobRecord, BlobStatus
from al1s.maa.types import QuickTestSessionStatus


class PostgresTerminalBlobAccessRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_authorized(self, terminal_id: UUID, blob_id: UUID) -> BlobRecord | None:
        now = datetime.now(UTC)
        formal_access = exists(
            select(TaskPackageRow.id)
            .join(
                ExecutionSnapshotBlobRow,
                ExecutionSnapshotBlobRow.snapshot_id == TaskPackageRow.snapshot_id,
            )
            .where(
                ExecutionSnapshotBlobRow.blob_id == blob_id,
                TaskPackageRow.terminal_id == terminal_id,
                TaskPackageRow.status.in_(
                    (PackageStatus.AVAILABLE.value, PackageStatus.ACCEPTED.value)
                ),
            )
        )
        quick_test_access = exists(
            select(MaaQuickTestBlobRow.session_id)
            .join(
                MaaQuickTestSessionRow,
                MaaQuickTestSessionRow.id == MaaQuickTestBlobRow.session_id,
            )
            .where(
                MaaQuickTestBlobRow.blob_id == blob_id,
                MaaQuickTestSessionRow.terminal_id == terminal_id,
                MaaQuickTestSessionRow.status.in_(
                    (
                        QuickTestSessionStatus.ISSUED.value,
                        QuickTestSessionStatus.CLAIMED.value,
                    )
                ),
                MaaQuickTestSessionRow.expires_at > now,
            )
        )
        statement = select(BlobObjectRow).where(
            BlobObjectRow.id == blob_id,
            BlobObjectRow.status == BlobStatus.READY.value,
            or_(formal_access, quick_test_access),
        )
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            return None
        return BlobRecord(
            blob_id=row.id,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            object_key=row.object_key,
            status=BlobStatus(row.status),
            row_version=row.row_version,
            created_at=row.created_at,
            ready_at=row.ready_at,
            deleted_at=row.deleted_at,
        )
