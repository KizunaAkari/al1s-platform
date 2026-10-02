from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.publication_impact import (
    DerivedStrategyPublication,
    MaaPublicationImpactCoordinator,
)
from al1s.maa.recovery import MaaRecoveryClosureResolver
from al1s.maa.types import (
    ApplicationRecord,
    QualificationKind,
    QualificationStatus,
    ScriptPublicationImpact,
    ScriptQualificationReceipt,
    ScriptRecord,
    ScriptVersionRecord,
)
from al1s.maa.validation import (
    VALIDATOR_VERSION,
    ActionRegistry,
)
from al1s.maa.version_service import MaaScriptVersionService

MaaUowFactory = Callable[[], MaaUnitOfWork]


@dataclass(frozen=True, slots=True)
class CandidateSaveResult:
    script: ScriptRecord
    version: ScriptVersionRecord
    static_receipt: ScriptQualificationReceipt
    reused_version: bool


@dataclass(frozen=True, slots=True)
class PublicationResult:
    script: ScriptRecord
    published_version: ScriptVersionRecord
    previous_version_id: UUID | None
    derived_strategy_version_ids: tuple[UUID, ...]
    impact_hash: str


class MaaScriptPublicationService:
    """Validate immutable versions for current saves and retain legacy candidate operations.

    Editor document saves call ``save_current_document_in_uow`` and do not require
    a quick-test qualification. The candidate publication methods below remain
    for historical candidate data and compatibility, not the editor save path.
    """

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        *,
        action_registry: ActionRegistry | None = None,
        impact_coordinator: MaaPublicationImpactCoordinator | None = None,
        recovery_resolver: MaaRecoveryClosureResolver | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._impact_coordinator = impact_coordinator or MaaPublicationImpactCoordinator()
        self._now = now or (lambda: datetime.now(UTC))
        self._versions = MaaScriptVersionService(
            action_registry=action_registry,
            recovery_resolver=recovery_resolver,
            now=self._now,
        )

    def preview_publication(
        self,
        *,
        script_id: UUID,
        candidate_version_id: UUID,
    ) -> ScriptPublicationImpact:
        with self._uow_factory() as uow:
            script = self._require_script(uow, script_id, for_update=False)
            version = self._require_candidate_version(uow, script, candidate_version_id)
            return self._impact_coordinator.preview(
                uow,
                script,
                version,
                lock_strategies=False,
            )

    def save_candidate(
        self,
        *,
        script_id: UUID,
        expected_script_version: int,
        manifest: dict[str, Any],
        correlation_id: UUID,
    ) -> CandidateSaveResult:
        now = self._now()
        with self._uow_factory() as uow:
            script = self._require_script(uow, script_id, for_update=True)
            application = self._require_application(uow, script.application_id)
            result = self.stage_candidate(
                uow,
                script=script,
                application=application,
                expected_script_version=expected_script_version,
                manifest=manifest,
                correlation_id=correlation_id,
                now=now,
                source="candidate_save",
            )
            uow.commit()
            return result

    def stage_candidate(
        self,
        uow: MaaUnitOfWork,
        *,
        script: ScriptRecord,
        application: ApplicationRecord,
        expected_script_version: int,
        manifest: dict[str, Any],
        correlation_id: UUID,
        now: datetime,
        source: str,
        force_new_version: bool = False,
    ) -> CandidateSaveResult:
        """Validate and stage a candidate inside the caller's active transaction."""
        prepared = self._versions.prepare(
            uow,
            script=script,
            application=application,
            expected_script_version=expected_script_version,
            manifest=manifest,
            correlation_id=correlation_id,
            now=now,
            source=source,
            force_new_version=force_new_version,
        )
        version = prepared.version
        updated = script
        if script.candidate_version_id != version.script_version_id:
            changed = uow.scripts.set_candidate(
                script.script_id,
                script.row_version,
                version.script_version_id,
                now,
            )
            if changed is None:
                raise MaaDomainError("stale_script", "Script changed concurrently", 409)
            updated = changed
            self._record_candidate_changed(
                uow,
                updated,
                version,
                correlation_id=correlation_id,
                occurred_at=now,
            )
        return CandidateSaveResult(
            updated, version, prepared.static_receipt, prepared.reused_version
        )

    def save_current_document_in_uow(
        self,
        uow: MaaUnitOfWork,
        *,
        script: ScriptRecord,
        application: ApplicationRecord,
        expected_script_version: int,
        manifest: dict[str, Any],
        correlation_id: UUID,
        now: datetime,
        source: str,
    ) -> ScriptRecord:
        """Validate, persist and activate a document atomically in the caller's UoW."""
        if script.candidate_version_id is not None:
            raise MaaDomainError(
                "legacy_candidate_requires_migration",
                "Old candidate requires explicit migration",
                409,
            )
        prepared = self._versions.prepare(
            uow,
            script=script,
            application=application,
            expected_script_version=expected_script_version,
            manifest=manifest,
            correlation_id=correlation_id,
            now=now,
            source=source,
        )
        saved = uow.scripts.activate_saved_version(
            script.script_id,
            expected_script_version,
            prepared.version.script_version_id,
            now,
        )
        if saved is None:
            raise MaaDomainError("stale_script", "Script changed concurrently", 409)
        self._record_published(
            uow,
            script,
            saved,
            prepared.version,
            correlation_id=correlation_id,
            occurred_at=now,
        )
        return saved

    def validate_candidate(
        self,
        *,
        script_id: UUID,
        candidate_version_id: UUID,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> ScriptQualificationReceipt:
        self._validate_idempotency_key(idempotency_key)
        now = self._now()
        with self._uow_factory() as uow:
            script = self._require_script(uow, script_id, for_update=True)
            version = self._require_candidate_version(uow, script, candidate_version_id)
            existing = uow.qualifications.find_by_idempotency(
                version.script_version_id,
                QualificationKind.STATIC_CHECK,
                idempotency_key,
            )
            if existing is not None:
                return existing
            application = self._require_application(uow, script.application_id)
            validation = self._versions.validate_manifest(
                uow,
                script,
                application,
                version.manifest,
                persisted_version_id=version.script_version_id,
            )
            receipt = self._versions.new_receipt(
                version=version,
                kind=QualificationKind.STATIC_CHECK,
                status=(
                    QualificationStatus.PASSED if validation.valid else QualificationStatus.FAILED
                ),
                idempotency_key=idempotency_key,
                terminal_id=None,
                target_device_id=None,
                executor_version=VALIDATOR_VERSION,
                error_code=None if validation.valid else validation.issues[0].code,
                diagnostic={
                    "issues": [
                        {
                            "code": issue.code,
                            "message": issue.message,
                            "pointer": issue.pointer,
                        }
                        for issue in validation.issues
                    ],
                    "validator_version": VALIDATOR_VERSION,
                },
                correlation_id=correlation_id,
                created_at=now,
            )
            uow.qualifications.add(receipt)
            self._record_qualification(uow, script, receipt, correlation_id, now)
            uow.commit()
            return receipt

    def record_quick_test_result(
        self,
        *,
        script_id: UUID,
        candidate_version_id: UUID,
        manifest_hash: str,
        terminal_id: UUID,
        target_device_id: UUID,
        executor_version: str,
        passed: bool,
        idempotency_key: str,
        correlation_id: UUID,
        error_code: str | None = None,
        diagnostic: dict[str, Any] | None = None,
        qualification_kind: QualificationKind = QualificationKind.QUICK_TEST,
    ) -> ScriptQualificationReceipt:
        """Record a result only after a trusted quick-test execution adapter calls it."""
        if qualification_kind not in (QualificationKind.QUICK_TEST, QualificationKind.STEP_TEST):
            raise ValueError("Execution result must be a full or single-step test")
        self._validate_idempotency_key(idempotency_key)
        if not executor_version.strip() or len(executor_version) > 100:
            raise MaaDomainError(
                "invalid_executor_version", "Executor version must contain 1 to 100 characters"
            )
        if passed and error_code is not None:
            raise MaaDomainError(
                "invalid_quick_test_result", "A passed quick test cannot contain an error code"
            )
        if not passed and (not error_code or len(error_code) > 100):
            raise MaaDomainError(
                "invalid_quick_test_result",
                "A failed quick test must contain an error code of at most 100 characters",
            )
        now = self._now()
        with self._uow_factory() as uow:
            script = self._require_script(uow, script_id, for_update=True)
            version = self._require_candidate_version(uow, script, candidate_version_id)
            if version.manifest_hash != manifest_hash:
                raise MaaDomainError(
                    "quick_test_hash_mismatch",
                    "Quick test result does not match the candidate content hash",
                    409,
                )
            existing = uow.qualifications.find_by_idempotency(
                version.script_version_id,
                qualification_kind,
                idempotency_key,
            )
            status = QualificationStatus.PASSED if passed else QualificationStatus.FAILED
            if existing is not None:
                self._require_same_quick_test_result(
                    existing,
                    status=status,
                    terminal_id=terminal_id,
                    target_device_id=target_device_id,
                    manifest_hash=manifest_hash,
                    executor_version=executor_version,
                    error_code=error_code,
                )
                return existing
            receipt = self._versions.new_receipt(
                version=version,
                kind=qualification_kind,
                status=status,
                idempotency_key=idempotency_key,
                terminal_id=terminal_id,
                target_device_id=target_device_id,
                executor_version=executor_version,
                error_code=error_code,
                diagnostic=diagnostic or {},
                correlation_id=correlation_id,
                created_at=now,
            )
            uow.qualifications.add(receipt)
            self._record_qualification(uow, script, receipt, correlation_id, now)
            uow.commit()
            return receipt

    def publish_candidate(
        self,
        *,
        script_id: UUID,
        candidate_version_id: UUID,
        expected_script_version: int,
        correlation_id: UUID,
        expected_impact_hash: str | None = None,
    ) -> PublicationResult:
        now = self._now()
        with self._uow_factory() as uow:
            result = self.publish_candidate_in_uow(
                uow,
                script_id=script_id,
                candidate_version_id=candidate_version_id,
                expected_script_version=expected_script_version,
                correlation_id=correlation_id,
                now=now,
                expected_impact_hash=expected_impact_hash,
            )
            uow.commit()
            return result

    def publish_candidate_in_uow(
        self,
        uow: MaaUnitOfWork,
        *,
        script_id: UUID,
        candidate_version_id: UUID,
        expected_script_version: int,
        correlation_id: UUID,
        now: datetime,
        expected_impact_hash: str | None = None,
    ) -> PublicationResult:
        """Publish inside the caller's transaction so replay evidence is atomic."""
        script = self._require_script(uow, script_id, for_update=True)
        if script.row_version != expected_script_version:
            raise MaaDomainError("stale_script", "Script changed concurrently", 409)
        version = self._require_candidate_version(uow, script, candidate_version_id)
        required = {QualificationKind.STATIC_CHECK, QualificationKind.QUICK_TEST}
        passed = uow.qualifications.has_passed(
            version.script_version_id,
            version.manifest_hash,
            tuple(required),
            static_executor_version=VALIDATOR_VERSION,
        )
        missing = sorted(kind.value for kind in required - passed)
        if missing:
            raise MaaDomainError(
                "script_publication_gate_incomplete",
                "Candidate has not passed every publication gate",
                409,
                context={
                    "candidate_version_id": str(version.script_version_id),
                    "manifest_hash": version.manifest_hash,
                    "missing_qualifications": missing,
                },
            )
        impact = self._impact_coordinator.preview(
            uow,
            script,
            version,
            lock_strategies=True,
        )
        if impact.confirmation_required and expected_impact_hash != impact.impact_hash:
            raise MaaDomainError(
                "publication_impact_confirmation_required",
                "Publication impact must be reviewed again before publishing",
                409,
                context={
                    "impact_hash": impact.impact_hash,
                    "affected_strategy_count": len(impact.affected_strategies),
                    "active_schedule_count": len(impact.active_schedules),
                },
            )
        derived = self._impact_coordinator.derive_strategy_versions(
            uow,
            impact,
            version,
            now=now,
        )
        updated = uow.scripts.publish_candidate(
            script.script_id,
            script.row_version,
            version.script_version_id,
            now,
        )
        if updated is None:
            raise MaaDomainError("stale_script", "Script changed concurrently", 409)
        self._record_published(
            uow,
            script,
            updated,
            version,
            correlation_id=correlation_id,
            occurred_at=now,
        )
        for item in derived:
            self._record_derived_strategy_published(
                uow,
                item,
                script_id=script.script_id,
                correlation_id=correlation_id,
                occurred_at=now,
            )
        return PublicationResult(
            updated,
            version,
            script.current_version_id,
            tuple(item.version.strategy_version_id for item in derived),
            impact.impact_hash,
        )

    @staticmethod
    def _require_script(uow: MaaUnitOfWork, script_id: UUID, *, for_update: bool) -> ScriptRecord:
        script = uow.scripts.get_active(script_id, for_update=for_update)
        if script is None:
            raise MaaDomainError("script_not_found", "Script was not found", 404)
        return script

    @staticmethod
    def _require_application(uow: MaaUnitOfWork, application_id: UUID) -> ApplicationRecord:
        application = uow.applications.get_active(application_id)
        if application is None:
            raise MaaDomainError("application_not_found", "Application was not found", 404)
        return application

    @staticmethod
    def _require_candidate_version(
        uow: MaaUnitOfWork, script: ScriptRecord, candidate_version_id: UUID
    ) -> ScriptVersionRecord:
        if candidate_version_id not in {script.candidate_version_id, script.current_version_id}:
            raise MaaDomainError(
                "candidate_version_changed",
                "Requested version is no longer the script candidate",
                409,
            )
        version = uow.script_versions.get(candidate_version_id)
        if version is None or version.script_id != script.script_id:
            raise MaaDomainError(
                "candidate_version_missing", "Script candidate version was not found", 409
            )
        return version

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not value.strip() or len(value) > 128:
            raise MaaDomainError(
                "invalid_idempotency_key",
                "Idempotency key must contain 1 to 128 characters",
            )

    @staticmethod
    def _require_same_quick_test_result(
        existing: ScriptQualificationReceipt,
        *,
        status: QualificationStatus,
        terminal_id: UUID,
        target_device_id: UUID,
        manifest_hash: str,
        executor_version: str,
        error_code: str | None,
    ) -> None:
        if (
            existing.status is not status
            or existing.terminal_id != terminal_id
            or existing.target_device_id != target_device_id
            or existing.manifest_hash != manifest_hash
            or existing.executor_version != executor_version
            or existing.error_code != error_code
        ):
            raise MaaDomainError(
                "quick_test_idempotency_conflict",
                "Quick test result key was already used for different content",
                409,
            )

    @staticmethod
    def _record_candidate_changed(
        uow: MaaUnitOfWork,
        script: ScriptRecord,
        version: ScriptVersionRecord,
        *,
        correlation_id: UUID,
        occurred_at: datetime,
    ) -> None:
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="operator",
                actor_id=None,
                action="maa.script.candidate.save",
                target_type="maa_script",
                target_id=script.script_id,
                correlation_id=correlation_id,
                details={
                    "candidate_version_id": str(version.script_version_id),
                    "manifest_hash": version.manifest_hash,
                    "row_version": script.row_version,
                },
                summary="Maa script candidate saved",
            )
        )
        uow.outbox.add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type="maa.script.validation-state-changed.v1",
                schema_version=1,
                aggregate_type="maa_script",
                aggregate_id=script.script_id,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                payload={
                    "script_id": str(script.script_id),
                    "candidate_version_id": str(version.script_version_id),
                    "manifest_hash": version.manifest_hash,
                    "state": "awaiting_quick_test",
                },
            )
        )

    @staticmethod
    def _record_qualification(
        uow: MaaUnitOfWork,
        script: ScriptRecord,
        receipt: ScriptQualificationReceipt,
        correlation_id: UUID,
        occurred_at: datetime,
    ) -> None:
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type=(
                    "terminal"
                    if receipt.kind
                    in (
                        QualificationKind.QUICK_TEST,
                        QualificationKind.STEP_TEST,
                    )
                    else "operator"
                ),
                actor_id=receipt.terminal_id,
                action=f"maa.script.{receipt.kind.value}",
                target_type="maa_script",
                target_id=script.script_id,
                correlation_id=correlation_id,
                details={
                    "receipt_id": str(receipt.receipt_id),
                    "script_version_id": str(receipt.script_version_id),
                    "manifest_hash": receipt.manifest_hash,
                    "status": receipt.status.value,
                    "error_code": receipt.error_code,
                },
                summary="Maa script qualification recorded",
            )
        )
        uow.outbox.add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type="maa.script.validation-state-changed.v1",
                schema_version=1,
                aggregate_type="maa_script",
                aggregate_id=script.script_id,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                payload={
                    "script_id": str(script.script_id),
                    "script_version_id": str(receipt.script_version_id),
                    "manifest_hash": receipt.manifest_hash,
                    "qualification": receipt.kind.value,
                    "status": receipt.status.value,
                },
            )
        )

    @staticmethod
    def _record_published(
        uow: MaaUnitOfWork,
        previous: ScriptRecord,
        published: ScriptRecord,
        version: ScriptVersionRecord,
        *,
        correlation_id: UUID,
        occurred_at: datetime,
    ) -> None:
        details = {
            "previous_version_id": (
                str(previous.current_version_id) if previous.current_version_id else None
            ),
            "published_version_id": str(version.script_version_id),
            "manifest_hash": version.manifest_hash,
            "row_version": published.row_version,
        }
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="operator",
                actor_id=None,
                action="maa.script.publish",
                target_type="maa_script",
                target_id=published.script_id,
                correlation_id=correlation_id,
                details=details,
                summary="Maa script version published",
            )
        )
        uow.outbox.add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type="maa.script.version-published.v1",
                schema_version=1,
                aggregate_type="maa_script",
                aggregate_id=published.script_id,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                payload={"script_id": str(published.script_id), **details},
            )
        )

    @staticmethod
    def _record_derived_strategy_published(
        uow: MaaUnitOfWork,
        derived: DerivedStrategyPublication,
        *,
        script_id: UUID,
        correlation_id: UUID,
        occurred_at: datetime,
    ) -> None:
        details = {
            "previous_version_id": str(derived.previous.current_version_id),
            "published_version_id": str(derived.version.strategy_version_id),
            "manifest_hash": derived.version.manifest_hash,
            "trigger_script_id": str(script_id),
            "row_version": derived.published.row_version,
        }
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="operator",
                actor_id=None,
                action="maa.strategy.derive_from_script_publication",
                target_type="maa_strategy",
                target_id=derived.published.strategy_id,
                correlation_id=correlation_id,
                details=details,
                summary="Maa strategy version derived from script publication",
            )
        )
        uow.outbox.add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type="maa.strategy.version-published.v1",
                schema_version=1,
                aggregate_type="maa_strategy",
                aggregate_id=derived.published.strategy_id,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                payload={
                    "strategy_id": str(derived.published.strategy_id),
                    "derivation": "script_publication",
                    **details,
                },
            )
        )
