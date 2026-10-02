from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import MutationReceiptRecord

MaaUowFactory = Callable[[], MaaUnitOfWork]
MutationCallback = Callable[[MaaUnitOfWork], "MutationOutcome"]
IntegrityErrorMapper = Callable[[IntegrityError], MaaDomainError]
DEFAULT_RECEIPT_RETENTION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    aggregate_type: str
    aggregate_id: UUID
    response_status: int
    response_body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MutationExecution:
    receipt: MutationReceiptRecord
    replayed: bool


class MaaMutationService:
    """Run one management mutation and its replay receipt in the same transaction."""

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        *,
        now: Callable[[], datetime] | None = None,
        retention: timedelta = DEFAULT_RECEIPT_RETENTION,
    ) -> None:
        if retention <= timedelta(0):
            raise ValueError("mutation receipt retention must be positive")
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._retention = retention

    def execute(
        self,
        *,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        mutate: MutationCallback,
        integrity_error_mapper: IntegrityErrorMapper | None = None,
    ) -> MutationExecution:
        normalized_operation = operation.strip()
        normalized_key = idempotency_key.strip()
        self._validate_identity(normalized_operation, normalized_key)
        try:
            request_hash = canonical_manifest_hash(request_payload)
        except (TypeError, ValueError) as exc:
            raise MaaDomainError(
                "invalid_mutation_request",
                "Mutation request must be JSON serializable",
                422,
            ) from exc

        try:
            with self._uow_factory() as uow:
                uow.mutation_receipts.acquire_idempotency_lock(normalized_operation, normalized_key)
                existing = uow.mutation_receipts.get_by_idempotency(
                    normalized_operation, normalized_key
                )
                if existing is not None:
                    return self._replay(existing, request_hash)
                outcome = mutate(uow)
                self._validate_outcome(outcome)
                now = self._now()
                receipt = MutationReceiptRecord(
                    receipt_id=uuid4(),
                    operation=normalized_operation,
                    idempotency_key=normalized_key,
                    request_hash=request_hash,
                    aggregate_type=outcome.aggregate_type,
                    aggregate_id=outcome.aggregate_id,
                    response_status=outcome.response_status,
                    response_body=outcome.response_body,
                    created_at=now,
                    expires_at=now + self._retention,
                )
                uow.mutation_receipts.add(receipt)
                uow.flush()
                uow.commit()
                return MutationExecution(receipt=receipt, replayed=False)
        except IntegrityError as exc:
            # A concurrent request with the same operation/key can win the unique
            # constraint after this transaction performed its tentative writes.
            # PostgreSQL rolls the complete losing transaction back; fetch and
            # verify the winner instead of repeating the business mutation.
            with self._uow_factory() as uow:
                existing = uow.mutation_receipts.get_by_idempotency(
                    normalized_operation, normalized_key
                )
                if existing is None:
                    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                    if constraint == "ck_maa_reference_active":
                        raise MaaDomainError(
                            "script_reference_changed",
                            "引用脚本已被删除。请刷新并重新选择。",
                            409,
                        ) from exc
                    if integrity_error_mapper is not None:
                        raise integrity_error_mapper(exc) from exc
                    raise
                return self._replay(existing, request_hash)

    @staticmethod
    def _validate_identity(operation: str, idempotency_key: str) -> None:
        if not 1 <= len(operation) <= 255:
            raise MaaDomainError(
                "invalid_mutation_operation",
                "Mutation operation must contain 1 to 255 characters",
                500,
            )
        if not 1 <= len(idempotency_key) <= 128:
            raise MaaDomainError(
                "invalid_idempotency_key",
                "Idempotency key must contain 1 to 128 characters",
                422,
            )

    @staticmethod
    def _validate_outcome(outcome: MutationOutcome) -> None:
        if not 1 <= len(outcome.aggregate_type) <= 64:
            raise RuntimeError("mutation aggregate type must contain 1 to 64 characters")
        if not 200 <= outcome.response_status <= 299:
            raise RuntimeError("mutation response status must be successful")
        try:
            canonical_manifest_hash(outcome.response_body)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("mutation response body must be JSON serializable") from exc

    @staticmethod
    def _replay(existing: MutationReceiptRecord, request_hash: str) -> MutationExecution:
        if existing.request_hash != request_hash:
            raise MaaDomainError(
                "idempotency_key_conflict",
                "Idempotency key was already used for a different request",
                409,
            )
        return MutationExecution(receipt=existing, replayed=True)
