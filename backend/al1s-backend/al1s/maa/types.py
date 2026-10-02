from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from al1s.execution.scheduling_types import ActiveContentScheduleImpact


class ScriptType(StrEnum):
    STANDARD = "standard"
    MODULE_START = "module_start"
    MODULE_PROCESS = "module_process"
    MODULE_END = "module_end"


class ScriptStatus(StrEnum):
    VALIDATION_PENDING = "validation_pending"
    ACTIVE = "active"
    RETIRED = "retired"


class ImportStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportItemStatus(StrEnum):
    IMPORTED = "imported"
    REJECTED = "rejected"


class QualificationKind(StrEnum):
    STATIC_CHECK = "static_check"
    QUICK_TEST = "quick_test"
    STEP_TEST = "step_test"


class QualificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class QuickTestSessionStatus(StrEnum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class StrategyStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class StrategyModuleRole(StrEnum):
    START = "start"
    PROCESS = "process"
    END = "end"


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    application_id: UUID
    package_name: str
    display_name: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    row_version: int
    icon_png: bytes | None = None


@dataclass(frozen=True, slots=True)
class ApplicationDeviceRecord:
    binding_id: UUID
    application_id: UUID
    device_id: UUID
    package_name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScriptRecord:
    script_id: UUID
    application_id: UUID
    name: str
    normalized_name: str
    script_type: ScriptType
    status: ScriptStatus
    current_version_id: UUID | None
    candidate_version_id: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class ScriptVersionRecord:
    script_version_id: UUID
    script_id: UUID
    revision: int
    schema_version: int
    manifest_hash: str
    manifest: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScriptBlobReference:
    script_version_id: UUID
    blob_id: UUID
    json_pointer: str
    resource_role: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class DeclaredBlobResource:
    blob_id: UUID
    sha256: str
    media_type: str
    size_bytes: int
    json_pointer: str
    resource_role: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class ScriptQualificationReceipt:
    receipt_id: UUID
    script_version_id: UUID
    manifest_hash: str
    kind: QualificationKind
    status: QualificationStatus
    idempotency_key: str
    terminal_id: UUID | None
    target_device_id: UUID | None
    executor_version: str
    error_code: str | None
    diagnostic: dict[str, Any]
    correlation_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class QuickTestSessionRecord:
    session_id: UUID
    script_id: UUID
    script_version_id: UUID
    manifest_hash: str
    definition_hash: str
    definition: dict[str, Any]
    terminal_id: UUID
    target_device_id: UUID
    status: QuickTestSessionStatus
    request_idempotency_key: str
    expires_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    qualification_receipt_id: UUID | None
    created_at: datetime
    row_version: int
    started_at: datetime | None = None
    cancel_requested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class QuickTestEventRecord:
    session_id: UUID
    sequence: int
    kind: str
    step_number: int | None
    code: str | None
    created_at: datetime
    rule_name: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    strategy_id: UUID
    application_id: UUID
    name: str
    normalized_name: str
    status: StrategyStatus
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class StrategyVersionRecord:
    strategy_version_id: UUID
    strategy_id: UUID
    revision: int
    schema_version: int
    manifest_hash: str
    manifest: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StrategyModuleRecord:
    strategy_version_id: UUID
    position: int
    module_role: StrategyModuleRole
    script_version_id: UUID
    wait_after_ms: int


@dataclass(frozen=True, slots=True)
class StrategyPublicationImpact:
    strategy_id: UUID
    current_version_id: UUID | None
    proposed_manifest_hash: str
    active_schedules: tuple[ActiveContentScheduleImpact, ...]
    impact_hash: str
    content_changed: bool

    @property
    def confirmation_required(self) -> bool:
        return self.content_changed and bool(self.active_schedules)


@dataclass(frozen=True, slots=True)
class ProcessModuleInput:
    script_id: UUID
    wait_after_ms: int = 0


@dataclass(frozen=True, slots=True)
class AffectedStrategyPublication:
    strategy_id: UUID
    strategy_name: str
    current_version_id: UUID
    row_version: int


@dataclass(frozen=True, slots=True)
class ScriptPublicationImpact:
    script_id: UUID
    current_version_id: UUID | None
    candidate_version_id: UUID
    candidate_manifest_hash: str
    affected_strategies: tuple[AffectedStrategyPublication, ...]
    active_schedules: tuple[ActiveContentScheduleImpact, ...]
    impact_hash: str

    @property
    def confirmation_required(self) -> bool:
        return bool(self.affected_strategies or self.active_schedules)


@dataclass(frozen=True, slots=True)
class ImportBatchRecord:
    batch_id: UUID
    logical_sha256: str
    archive_sha256: str
    archive_schema: str
    status: ImportStatus
    script_count: int
    application_count: int
    resource_reference_count: int
    unique_resource_count: int
    error_code: str | None
    diagnostic: str | None
    created_at: datetime
    completed_at: datetime | None
    row_version: int
    strategy_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class NewImportItem:
    item_id: UUID
    batch_id: UUID
    archive_ordinal: int
    script_name: str
    application_package: str | None
    script_id: UUID | None
    script_version_id: UUID | None
    status: ImportItemStatus
    migration_code: str | None
    error_code: str | None
    diagnostic: str | None


@dataclass(frozen=True, slots=True)
class ImportItemRecord:
    item_id: UUID
    batch_id: UUID
    archive_ordinal: int
    script_name: str
    application_package: str | None
    script_id: UUID | None
    script_version_id: UUID | None
    status: ImportItemStatus
    migration_code: str | None
    error_code: str | None
    diagnostic: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ImportResult:
    batch: ImportBatchRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class MutationReceiptRecord:
    receipt_id: UUID
    operation: str
    idempotency_key: str
    request_hash: str
    aggregate_type: str
    aggregate_id: UUID
    response_status: int
    response_body: dict[str, Any]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    pointer: str


@dataclass(frozen=True, slots=True)
class ScriptValidationResult:
    script_type: ScriptType | None
    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues
