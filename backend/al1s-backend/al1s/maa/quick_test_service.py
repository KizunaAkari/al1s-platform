from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from al1s.execution.scheduling_types import (
    CapabilityRequirements,
    ResolvedExecutionDefinition,
    SnapshotBlobReference,
)
from al1s.maa.definition_provider import MaaExecutionDefinitionProvider
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.quick_test_rule_events import project_rule_event
from al1s.maa.single_step import select_step
from al1s.maa.types import (
    QualificationKind,
    QuickTestEventRecord,
    QuickTestSessionRecord,
    QuickTestSessionStatus,
    ScriptQualificationReceipt,
)

MaaUowFactory = Callable[[], MaaUnitOfWork]


@dataclass(frozen=True, slots=True)
class IssuedQuickTest:
    session: QuickTestSessionRecord
    definition: ResolvedExecutionDefinition


@dataclass(frozen=True, slots=True)
class CompletedQuickTest:
    session: QuickTestSessionRecord
    receipt: ScriptQualificationReceipt


@dataclass(frozen=True, slots=True)
class PendingQuickTest:
    session: QuickTestSessionRecord


@dataclass(frozen=True, slots=True)
class QuickTestDetail:
    session: QuickTestSessionRecord
    receipt: ScriptQualificationReceipt | None


@dataclass(frozen=True, slots=True)
class ClaimedQuickTest:
    session: QuickTestSessionRecord
    definition: ResolvedExecutionDefinition


class MaaQuickTestService:
    """Issue terminal-bound tests and accept only results for the issued definition."""

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        definition_provider: MaaExecutionDefinitionProvider,
        publication_service: MaaScriptPublicationService,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._definitions = definition_provider
        self._publication = publication_service
        self._now = now or (lambda: datetime.now(UTC))

    def issue(
        self,
        *,
        script_id: UUID,
        candidate_version_id: UUID,
        terminal_id: UUID,
        target_device_id: UUID,
        idempotency_key: str,
        ttl: timedelta,
        step_number: int | None = None,
    ) -> IssuedQuickTest:
        self._validate_idempotency_key(idempotency_key)
        self._definitions.authorize_target(f"script:{script_id}", target_device_id)
        if not timedelta(minutes=1) <= ttl <= timedelta(hours=4):
            raise MaaDomainError(
                "invalid_quick_test_ttl",
                "Quick-test TTL must be between 60 seconds and 4 hours",
                422,
            )
        with self._uow_factory() as uow:
            existing = uow.quick_tests.find_by_idempotency(candidate_version_id, idempotency_key)
            if existing is not None:
                self._require_same_issue(
                    existing,
                    script_id=script_id,
                    terminal_id=terminal_id,
                    target_device_id=target_device_id,
                    step_number=step_number,
                )
                return IssuedQuickTest(existing, _definition_from_json(existing.definition))
        definition = self._definitions.resolve_candidate(script_id, candidate_version_id)
        if step_number is not None:
            definition = select_step(definition, step_number)
        manifest_hash = self._candidate_hash(definition)
        now = self._now()
        frozen_definition = _definition_to_json(definition)
        with self._uow_factory() as uow:
            session = QuickTestSessionRecord(
                session_id=uuid4(),
                script_id=script_id,
                script_version_id=candidate_version_id,
                manifest_hash=manifest_hash,
                definition_hash=definition.manifest_hash,
                definition=frozen_definition,
                terminal_id=terminal_id,
                target_device_id=target_device_id,
                status=QuickTestSessionStatus.ISSUED,
                request_idempotency_key=idempotency_key,
                expires_at=now + ttl,
                claimed_at=None,
                completed_at=None,
                qualification_receipt_id=None,
                created_at=now,
                row_version=1,
            )
            uow.quick_tests.add(session, definition.blobs)
            uow.commit()
            return IssuedQuickTest(session, definition)

    def get_detail(self, *, script_id: UUID, session_id: UUID) -> QuickTestDetail:
        """Read one session and, when present, its result; never expose the definition."""
        with self._uow_factory() as uow:
            session = uow.quick_tests.get(session_id, for_update=False)
            if session is None or session.script_id != script_id:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            receipt = (
                uow.qualifications.get(session.qualification_receipt_id)
                if session.qualification_receipt_id is not None else None
            )
            if session.status is QuickTestSessionStatus.COMPLETED and receipt is None:
                raise MaaDomainError(
                    "quick_test_receipt_missing", "Completed quick-test receipt is unavailable", 409
                )
            return QuickTestDetail(session, receipt)

    def list_pending(self, terminal_id: UUID, *, limit: int) -> tuple[PendingQuickTest, ...]:
        if not 1 <= limit <= 50:
            raise MaaDomainError(
                "invalid_quick_test_limit", "Quick-test limit must be between 1 and 50", 422
            )
        with self._uow_factory() as uow:
            sessions = uow.quick_tests.list_pending(terminal_id, self._now(), limit=limit)
        return tuple(PendingQuickTest(session) for session in sessions)

    def request_cancel(self, *, script_id: UUID, session_id: UUID) -> QuickTestSessionRecord:
        with self._uow_factory() as uow:
            session = uow.quick_tests.get(session_id, for_update=True)
            if session is None or session.script_id != script_id:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            if session.cancel_requested_at is not None or session.status in (
                QuickTestSessionStatus.COMPLETED, QuickTestSessionStatus.EXPIRED,
                QuickTestSessionStatus.CANCELLED,
            ):
                return session
            changed = uow.quick_tests.request_cancel(
                session_id, session.row_version, self._now()
            )
            if changed is None:
                raise MaaDomainError(
                    "quick_test_session_conflict", "Quick-test session changed concurrently", 409
                )
            uow.commit()
            return changed

    def get_control(
        self, *, session_id: UUID, terminal_id: UUID
    ) -> QuickTestSessionRecord:
        with self._uow_factory() as uow:
            session = uow.quick_tests.get(session_id)
            if session is None or session.terminal_id != terminal_id:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            return session

    def report_events(
        self, *, session_id: UUID, terminal_id: UUID,
        events: tuple[QuickTestEventRecord, ...],
    ) -> int:
        if not 1 <= len(events) <= 50 or any(
            event.session_id != session_id
            or event.sequence < 1 or event.sequence > 1000
            or event.kind not in {"started", "step_started", "step_succeeded", "step_failed", "log"}
            or (event.step_number is not None and not 1 <= event.step_number <= 1000)
            or (event.code is not None and (len(event.code) > 100 or not event.code.isascii()))
            for event in events
        ):
            raise MaaDomainError("invalid_quick_test_events", "Quick-test events are invalid", 422)
        if len({event.sequence for event in events}) != len(events):
            raise MaaDomainError("invalid_quick_test_events", "Duplicate event sequence", 422)
        with self._uow_factory() as uow:
            session = uow.quick_tests.get(session_id, for_update=True)
            if session is None or session.terminal_id != terminal_id:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            if session.status not in (QuickTestSessionStatus.CLAIMED,
                                      QuickTestSessionStatus.COMPLETED):
                raise MaaDomainError(
                    "quick_test_session_conflict", "Quick-test session is not running", 409
                )
            now = self._now()
            if (session.completed_at is not None
                    and session.completed_at <= now - timedelta(days=7)):
                raise MaaDomainError(
                    "quick_test_event_window_closed", "Debug event window has closed", 409
                )
            if any(event.created_at.tzinfo is None
                   or event.created_at < session.created_at - timedelta(minutes=5)
                   or event.created_at > now + timedelta(minutes=5)
                   for event in events):
                raise MaaDomainError("invalid_quick_test_events", "Event time is invalid", 422)
            try:
                last = uow.quick_tests.append_events(
                    session_id, events, now - timedelta(days=7)
                )
            except ValueError as exc:
                raise MaaDomainError(str(exc), "Quick-test event sequence conflict", 409) from exc
            uow.commit()
            return last

    def list_events(
        self, *, script_id: UUID, session_id: UUID, after: int, limit: int
    ) -> tuple[QuickTestEventRecord, ...]:
        if not 0 <= after <= 1000 or not 1 <= limit <= 100:
            raise MaaDomainError("invalid_quick_test_event_page", "Page is invalid", 422)
        with self._uow_factory() as uow:
            session = uow.quick_tests.get(session_id)
            if session is None or session.script_id != script_id:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            return tuple(project_rule_event(event, session.definition) for event in
                         uow.quick_tests.list_events(session_id, after=after, limit=limit))

    def claim(self, session_id: UUID, terminal_id: UUID) -> ClaimedQuickTest:
        now = self._now()
        with self._uow_factory() as uow:
            session = uow.quick_tests.get(session_id, for_update=True)
            if session is None or session.terminal_id != terminal_id:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            if session.status is QuickTestSessionStatus.CLAIMED:
                return ClaimedQuickTest(session, _definition_from_json(session.definition))
            if session.status is QuickTestSessionStatus.COMPLETED:
                raise MaaDomainError(
                    "quick_test_session_completed", "Quick-test session is already complete", 409
                )
            if session.status is QuickTestSessionStatus.EXPIRED or session.expires_at <= now:
                if session.status is QuickTestSessionStatus.ISSUED:
                    uow.quick_tests.expire(session.session_id, session.row_version, now)
                    uow.commit()
                raise MaaDomainError(
                    "quick_test_session_expired", "Quick-test session has expired", 409
                )
            claimed = uow.quick_tests.claim(
                session.session_id,
                session.row_version,
                terminal_id,
                now,
            )
            if claimed is None:
                raise MaaDomainError(
                    "quick_test_session_conflict",
                    "Quick-test session changed concurrently",
                    409,
                )
            uow.commit()
            return ClaimedQuickTest(claimed, _definition_from_json(claimed.definition))

    def record_result(
        self,
        *,
        session_id: UUID,
        script_id: UUID,
        authenticated_terminal_id: UUID,
        candidate_version_id: UUID,
        manifest_hash: str,
        definition_hash: str,
        executor_version: str,
        passed: bool,
        idempotency_key: str,
        correlation_id: UUID,
        error_code: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> CompletedQuickTest:
        now = self._now()
        with self._uow_factory() as uow:
            session = uow.quick_tests.get(session_id, for_update=True)
            if session is None or session.script_id != script_id:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            self._require_result_identity(
                session,
                terminal_id=authenticated_terminal_id,
                candidate_version_id=candidate_version_id,
                manifest_hash=manifest_hash,
                definition_hash=definition_hash,
            )
            if session.status is QuickTestSessionStatus.EXPIRED or session.expires_at <= now:
                if session.status in (
                    QuickTestSessionStatus.ISSUED,
                    QuickTestSessionStatus.CLAIMED,
                ):
                    uow.quick_tests.expire(session.session_id, session.row_version, now)
                    uow.commit()
                raise MaaDomainError(
                    "quick_test_session_expired", "Quick-test session has expired", 409
                )
            if session.status is QuickTestSessionStatus.COMPLETED:
                assert session.qualification_receipt_id is not None
                receipt = uow.qualifications.get(session.qualification_receipt_id)
                if receipt is None:
                    raise MaaDomainError(
                        "quick_test_receipt_missing",
                        "Completed quick-test receipt is unavailable",
                        409,
                    )
                self._require_same_completed_result(
                    receipt,
                    executor_version=executor_version,
                    passed=passed,
                    idempotency_key=idempotency_key,
                    error_code=error_code,
                )
                return CompletedQuickTest(session, receipt)
            if session.status is not QuickTestSessionStatus.CLAIMED:
                raise MaaDomainError(
                    "quick_test_claim_required",
                    "Quick-test session must be claimed before a result is submitted",
                    409,
                )

        receipt = self._publication.record_quick_test_result(
            script_id=script_id,
            candidate_version_id=candidate_version_id,
            manifest_hash=manifest_hash,
            terminal_id=authenticated_terminal_id,
            target_device_id=session.target_device_id,
            executor_version=executor_version,
            passed=passed,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            error_code=error_code,
            diagnostic=diagnostic,
            qualification_kind=(QualificationKind.STEP_TEST
                if session.definition["manifest"].get("debug_step_number") is not None
                else QualificationKind.QUICK_TEST),
        )
        with self._uow_factory() as uow:
            current = uow.quick_tests.get(session_id, for_update=True)
            if current is None:
                raise MaaDomainError(
                    "quick_test_session_not_found", "Quick-test session was not found", 404
                )
            if current.status is QuickTestSessionStatus.COMPLETED:
                if current.qualification_receipt_id != receipt.receipt_id:
                    raise MaaDomainError(
                        "quick_test_session_conflict",
                        "Quick-test session was completed by another result",
                        409,
                    )
                return CompletedQuickTest(current, receipt)
            completed = uow.quick_tests.complete(
                current.session_id,
                current.row_version,
                receipt.receipt_id,
                now,
            )
            if completed is None:
                raise MaaDomainError(
                    "quick_test_session_conflict",
                    "Quick-test session changed concurrently",
                    409,
                )
            uow.commit()
            return CompletedQuickTest(completed, receipt)

    @staticmethod
    def _candidate_hash(definition: ResolvedExecutionDefinition) -> str:
        value = definition.manifest.get("candidate_manifest_hash")
        if not isinstance(value, str):
            raise MaaDomainError(
                "quick_test_definition_invalid",
                "Quick-test definition is missing the candidate content hash",
                500,
            )
        return value

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not value.strip() or len(value) > 128:
            raise MaaDomainError(
                "invalid_idempotency_key",
                "Idempotency key must contain 1 to 128 characters",
                422,
            )

    @staticmethod
    def _require_same_issue(
        session: QuickTestSessionRecord,
        *,
        script_id: UUID,
        terminal_id: UUID,
        target_device_id: UUID,
        step_number: int | None = None,
    ) -> None:
        if (
            session.script_id != script_id
            or session.terminal_id != terminal_id
            or session.target_device_id != target_device_id
            or session.definition["manifest"].get("debug_step_number") != step_number
        ):
            raise MaaDomainError(
                "quick_test_idempotency_conflict",
                "Idempotency key was already used for a different quick test",
                409,
            )

    @staticmethod
    def _require_result_identity(
        session: QuickTestSessionRecord,
        *,
        terminal_id: UUID,
        candidate_version_id: UUID,
        manifest_hash: str,
        definition_hash: str,
    ) -> None:
        if session.terminal_id != terminal_id:
            raise MaaDomainError(
                "quick_test_terminal_mismatch",
                "Quick-test result came from a different terminal",
                403,
            )
        if (
            session.script_version_id != candidate_version_id
            or session.manifest_hash != manifest_hash
            or session.definition_hash != definition_hash
        ):
            raise MaaDomainError(
                "quick_test_identity_mismatch",
                "Quick-test result does not match the issued immutable definition",
                409,
            )

    @staticmethod
    def _require_same_completed_result(
        receipt: ScriptQualificationReceipt,
        *,
        executor_version: str,
        passed: bool,
        idempotency_key: str,
        error_code: str | None,
    ) -> None:
        expected_status = "passed" if passed else "failed"
        if (
            receipt.status.value != expected_status
            or receipt.executor_version != executor_version
            or receipt.idempotency_key != idempotency_key
            or receipt.error_code != error_code
        ):
            raise MaaDomainError(
                "quick_test_session_conflict",
                "Quick-test session was already completed by a different result",
                409,
            )


def _definition_to_json(definition: ResolvedExecutionDefinition) -> dict[str, Any]:
    requirements = definition.capability_requirements
    return {
        "revision_id": definition.revision_id,
        "schema_version": definition.schema_version,
        "manifest_hash": definition.manifest_hash,
        "manifest": definition.manifest,
        "capability_requirements": {
            "schema_version": requirements.schema_version,
            "min_protocol_version": requirements.min_protocol_version,
            "architectures": list(requirements.architectures),
            "min_memory_bytes": requirements.min_memory_bytes,
            "min_storage_bytes": requirements.min_storage_bytes,
            "accelerator_type": requirements.accelerator_type,
            "provider_keys": list(requirements.provider_keys),
            "requires_target_device": requirements.requires_target_device,
        },
        "blobs": [
            {
                "blob_id": str(item.blob_id),
                "resource_key": item.resource_key,
                "role": item.role,
            }
            for item in definition.blobs
        ],
    }


def _definition_from_json(value: dict[str, Any]) -> ResolvedExecutionDefinition:
    requirements = value["capability_requirements"]
    blobs = value["blobs"]
    return ResolvedExecutionDefinition(
        revision_id=str(value["revision_id"]),
        schema_version=int(value["schema_version"]),
        manifest_hash=str(value["manifest_hash"]),
        manifest=dict(value["manifest"]),
        capability_requirements=CapabilityRequirements(
            schema_version=int(requirements["schema_version"]),
            min_protocol_version=int(requirements["min_protocol_version"]),
            architectures=tuple(str(item) for item in requirements["architectures"]),
            min_memory_bytes=int(requirements["min_memory_bytes"]),
            min_storage_bytes=int(requirements["min_storage_bytes"]),
            accelerator_type=(
                str(requirements["accelerator_type"])
                if requirements["accelerator_type"] is not None
                else None
            ),
            provider_keys=tuple(str(item) for item in requirements["provider_keys"]),
            requires_target_device=bool(requirements["requires_target_device"]),
        ),
        blobs=tuple(
            SnapshotBlobReference(
                blob_id=UUID(str(item["blob_id"])),
                resource_key=str(item["resource_key"]),
                role=str(item["role"]),
            )
            for item in blobs
        ),
    )
