"""Maa persistence for PostgresMaaMutationReceiptRepository."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaMutationReceiptRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _mutation_receipt_record,
)
from al1s.maa.types import (
    MutationReceiptRecord,
)


class PostgresMaaMutationReceiptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire_idempotency_lock(self, operation: str, idempotency_key: str) -> None:
        identity = f"{operation}\x1f{idempotency_key}"
        self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(identity, 0)))
        )

    def get_by_idempotency(
        self, operation: str, idempotency_key: str
    ) -> MutationReceiptRecord | None:
        statement = select(MaaMutationReceiptRow).where(
            MaaMutationReceiptRow.operation == operation,
            MaaMutationReceiptRow.idempotency_key == idempotency_key,
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _mutation_receipt_record(row)

    def add(self, receipt: MutationReceiptRecord) -> None:
        self._session.add(
            MaaMutationReceiptRow(
                id=receipt.receipt_id,
                operation=receipt.operation,
                idempotency_key=receipt.idempotency_key,
                request_hash=receipt.request_hash,
                aggregate_type=receipt.aggregate_type,
                aggregate_id=receipt.aggregate_id,
                response_status=receipt.response_status,
                response_body=receipt.response_body,
                created_at=receipt.created_at,
                expires_at=receipt.expires_at,
            )
        )
