"""Maa persistence for PostgresMaaQualificationRepository."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaScriptQualificationReceiptRow,
)
from al1s.maa.types import (
    QualificationKind,
    QualificationStatus,
    ScriptQualificationReceipt,
)


class PostgresMaaQualificationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, receipt_id: UUID) -> ScriptQualificationReceipt | None:
        statement = select(MaaScriptQualificationReceiptRow).where(
            MaaScriptQualificationReceiptRow.id == receipt_id
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else self._record(row)

    def add(self, receipt: ScriptQualificationReceipt) -> None:
        self.add_many([receipt])

    def add_many(self, receipts: Sequence[ScriptQualificationReceipt]) -> None:
        self._session.add_all(
            [
                MaaScriptQualificationReceiptRow(
                    id=receipt.receipt_id,
                    script_version_id=receipt.script_version_id,
                    manifest_hash=receipt.manifest_hash,
                    kind=receipt.kind.value,
                    status=receipt.status.value,
                    idempotency_key=receipt.idempotency_key,
                    terminal_id=receipt.terminal_id,
                    target_device_id=receipt.target_device_id,
                    executor_version=receipt.executor_version,
                    error_code=receipt.error_code,
                    diagnostic=receipt.diagnostic,
                    correlation_id=receipt.correlation_id,
                    created_at=receipt.created_at,
                )
                for receipt in receipts
            ]
        )

    def find_by_idempotency(
        self,
        script_version_id: UUID,
        kind: QualificationKind,
        idempotency_key: str,
    ) -> ScriptQualificationReceipt | None:
        statement = select(MaaScriptQualificationReceiptRow).where(
            MaaScriptQualificationReceiptRow.script_version_id == script_version_id,
            MaaScriptQualificationReceiptRow.kind == kind.value,
            MaaScriptQualificationReceiptRow.idempotency_key == idempotency_key,
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else self._record(row)

    @staticmethod
    def _record(row: MaaScriptQualificationReceiptRow) -> ScriptQualificationReceipt:
        return ScriptQualificationReceipt(
            receipt_id=row.id,
            script_version_id=row.script_version_id,
            manifest_hash=row.manifest_hash,
            kind=QualificationKind(row.kind),
            status=QualificationStatus(row.status),
            idempotency_key=row.idempotency_key,
            terminal_id=row.terminal_id,
            target_device_id=row.target_device_id,
            executor_version=row.executor_version,
            error_code=row.error_code,
            diagnostic=row.diagnostic,
            correlation_id=row.correlation_id,
            created_at=row.created_at,
        )

    def has_passed(
        self,
        script_version_id: UUID,
        manifest_hash: str,
        kinds: Sequence[QualificationKind],
        *,
        static_executor_version: str,
    ) -> set[QualificationKind]:
        if not kinds:
            return set()
        statement = select(MaaScriptQualificationReceiptRow.kind).where(
            MaaScriptQualificationReceiptRow.script_version_id == script_version_id,
            MaaScriptQualificationReceiptRow.manifest_hash == manifest_hash,
            MaaScriptQualificationReceiptRow.kind.in_([kind.value for kind in kinds]),
            MaaScriptQualificationReceiptRow.status == QualificationStatus.PASSED.value,
            or_(
                MaaScriptQualificationReceiptRow.kind != QualificationKind.STATIC_CHECK.value,
                MaaScriptQualificationReceiptRow.executor_version == static_executor_version,
            ),
        )
        return {
            QualificationKind(value) for value in self._session.execute(statement).scalars().all()
        }
