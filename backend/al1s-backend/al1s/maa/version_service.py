"""Prepare immutable script versions without changing a script's lifecycle pointer."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.recovery import MaaRecoveryClosureResolver
from al1s.maa.types import (
    ApplicationRecord,
    QualificationKind,
    QualificationStatus,
    ScriptBlobReference,
    ScriptQualificationReceipt,
    ScriptRecord,
    ScriptValidationResult,
    ScriptVersionRecord,
    ValidationIssue,
)
from al1s.maa.validation import (
    VALIDATOR_VERSION,
    ActionRegistry,
    declared_blob_resources,
    validate_script_document,
)


@dataclass(frozen=True, slots=True)
class PreparedScriptVersion:
    version: ScriptVersionRecord
    static_receipt: ScriptQualificationReceipt
    reused_version: bool


class MaaScriptVersionService:
    def __init__(
        self,
        *,
        action_registry: ActionRegistry | None = None,
        recovery_resolver: MaaRecoveryClosureResolver | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._action_registry = action_registry or ActionRegistry()
        self._recovery_resolver = recovery_resolver or MaaRecoveryClosureResolver()
        self._now = now or (lambda: datetime.now(UTC))

    def prepare(
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
    ) -> PreparedScriptVersion:
        if script.row_version != expected_script_version:
            raise MaaDomainError(
                "stale_script",
                "Script changed concurrently",
                409,
                context={"expected_version": expected_script_version},
            )
        document = copy.deepcopy(manifest)
        manifest_hash = canonical_manifest_hash(document)
        validation = self.validate_manifest(uow, script, application, document)
        self._raise_validation_failure(validation)

        version = (
            None
            if force_new_version
            else uow.script_versions.find_by_hash(script.script_id, manifest_hash)
        )
        reused = version is not None
        if version is None:
            version = ScriptVersionRecord(
                script_version_id=uuid4(),
                script_id=script.script_id,
                revision=uow.script_versions.next_revision(script.script_id),
                schema_version=2,
                manifest_hash=manifest_hash,
                manifest=document,
                created_at=now,
            )
            uow.script_versions.add_many([version])
            uow.flush()
            uow.script_versions.add_blob_references(
                self._blob_references(version.script_version_id, document)
            )

        static_receipt = self.ensure_static_pass(
            uow,
            version,
            correlation_id=correlation_id,
            now=now,
            source=source,
        )
        return PreparedScriptVersion(version, static_receipt, reused)

    def validate_manifest(
        self,
        uow: MaaUnitOfWork,
        script: ScriptRecord,
        application: ApplicationRecord,
        manifest: dict[str, Any],
        *,
        persisted_version_id: UUID | None = None,
    ) -> ScriptValidationResult:
        result = validate_script_document(manifest, self._action_registry)
        issues = list(result.issues)
        if result.script_type is not None and result.script_type is not script.script_type:
            issues.append(
                ValidationIssue(
                    "script_type_mismatch",
                    "Candidate script type differs from its script record",
                    "/script_type",
                )
            )
        target = manifest.get("target")
        if (
            isinstance(target, dict)
            and target.get("application_package") != application.package_name
        ):
            issues.append(
                ValidationIssue(
                    "script_application_mismatch",
                    "Candidate target differs from its application category",
                    "/target/application_package",
                )
            )
        if not issues:
            issues.extend(
                self._resource_issues(
                    uow,
                    manifest,
                    persisted_version_id=persisted_version_id,
                )
            )
        if not issues:
            issues.extend(
                self._recovery_issues(
                    uow,
                    script,
                    application,
                    manifest,
                    persisted_version_id=persisted_version_id,
                )
            )
        return ScriptValidationResult(result.script_type, tuple(issues))

    def _recovery_issues(
        self,
        uow: MaaUnitOfWork,
        script: ScriptRecord,
        application: ApplicationRecord,
        manifest: dict[str, Any],
        *,
        persisted_version_id: UUID | None,
    ) -> list[ValidationIssue]:
        version = ScriptVersionRecord(
            script_version_id=persisted_version_id or UUID(int=0),
            script_id=script.script_id,
            revision=0,
            schema_version=2,
            manifest_hash=canonical_manifest_hash(manifest),
            manifest=manifest,
            created_at=self._now(),
        )
        try:
            self._recovery_resolver.resolve(
                uow,
                {script.script_id: version},
                application_id=application.application_id,
                include_references=False,
            )
        except MaaDomainError as exc:
            return [ValidationIssue(exc.code, exc.message, "/steps")]
        return []

    @staticmethod
    def _resource_issues(
        uow: MaaUnitOfWork,
        manifest: dict[str, Any],
        *,
        persisted_version_id: UUID | None,
    ) -> list[ValidationIssue]:
        declared = declared_blob_resources(manifest)
        ready = uow.blobs.find_ready_by_sha256_many([item.sha256 for item in declared])
        issues: list[ValidationIssue] = []
        for item in declared:
            blob = ready.get(item.sha256)
            if blob is None:
                issues.append(
                    ValidationIssue(
                        "required_resource_missing",
                        "Candidate resource is not ready",
                        item.json_pointer,
                    )
                )
            elif (
                blob.blob_id != item.blob_id
                or blob.size_bytes != item.size_bytes
                or blob.media_type.lower() != item.media_type.lower()
            ):
                issues.append(
                    ValidationIssue(
                        "resource_metadata_mismatch",
                        "Candidate resource metadata differs from the Blob catalog",
                        item.json_pointer,
                    )
                )
        if persisted_version_id is not None:
            expected = [
                (item.blob_id, item.json_pointer, item.resource_role, item.ordinal)
                for item in declared
            ]
            stored = [
                (item.blob_id, item.json_pointer, item.resource_role, item.ordinal)
                for item in uow.script_versions.list_blob_references(persisted_version_id)
            ]
            if stored != expected:
                issues.append(
                    ValidationIssue(
                        "resource_reference_mismatch",
                        "Candidate resource references differ from its immutable manifest",
                        "/",
                    )
                )
        return issues

    @staticmethod
    def _blob_references(
        script_version_id: UUID, manifest: dict[str, Any]
    ) -> list[ScriptBlobReference]:
        return [
            ScriptBlobReference(
                script_version_id=script_version_id,
                blob_id=item.blob_id,
                json_pointer=item.json_pointer,
                resource_role=item.resource_role,
                ordinal=item.ordinal,
            )
            for item in declared_blob_resources(manifest)
        ]

    def ensure_static_pass(
        self,
        uow: MaaUnitOfWork,
        version: ScriptVersionRecord,
        *,
        correlation_id: UUID,
        now: datetime,
        source: str,
    ) -> ScriptQualificationReceipt:
        idempotency_key = f"static:{VALIDATOR_VERSION}:{version.manifest_hash}"
        existing = uow.qualifications.find_by_idempotency(
            version.script_version_id,
            QualificationKind.STATIC_CHECK,
            idempotency_key,
        )
        if existing is not None:
            if existing.status is not QualificationStatus.PASSED:
                raise MaaDomainError(
                    "static_check_receipt_conflict",
                    "The same validator previously rejected this immutable version",
                    409,
                )
            return existing
        receipt = self.new_receipt(
            version=version,
            kind=QualificationKind.STATIC_CHECK,
            status=QualificationStatus.PASSED,
            idempotency_key=idempotency_key,
            terminal_id=None,
            target_device_id=None,
            executor_version=VALIDATOR_VERSION,
            error_code=None,
            diagnostic={"issue_count": 0, "source": source},
            correlation_id=correlation_id,
            created_at=now,
        )
        uow.qualifications.add(receipt)
        return receipt

    @staticmethod
    def new_receipt(
        *,
        version: ScriptVersionRecord,
        kind: QualificationKind,
        status: QualificationStatus,
        idempotency_key: str,
        terminal_id: UUID | None,
        target_device_id: UUID | None,
        executor_version: str,
        error_code: str | None,
        diagnostic: dict[str, Any],
        correlation_id: UUID,
        created_at: datetime,
    ) -> ScriptQualificationReceipt:
        return ScriptQualificationReceipt(
            receipt_id=uuid4(),
            script_version_id=version.script_version_id,
            manifest_hash=version.manifest_hash,
            kind=kind,
            status=status,
            idempotency_key=idempotency_key,
            terminal_id=terminal_id,
            target_device_id=target_device_id,
            executor_version=executor_version,
            error_code=error_code,
            diagnostic=diagnostic,
            correlation_id=correlation_id,
            created_at=created_at,
        )

    @staticmethod
    def _raise_validation_failure(result: ScriptValidationResult) -> None:
        if result.valid:
            return
        first = result.issues[0]
        raise MaaDomainError(
            first.code,
            f"Candidate failed static validation at {first.pointer}",
            422,
            context={
                "pointer": first.pointer,
                "issues": [
                    {"code": item.code, "message": item.message, "pointer": item.pointer}
                    for item in result.issues
                ],
            },
        )
