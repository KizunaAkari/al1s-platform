from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from al1s.execution.offline_permits import OfflineStartPermitGrant
from al1s.execution.scheduling_types import (
    ExecutionAttemptRecord,
    ExecutionRecord,
)
from al1s.execution.types import ExecutionLeaseRecord


class PackageStatus(StrEnum):
    AVAILABLE = "available"
    ACCEPTED = "accepted"
    DELIVERY_FAILED = "delivery_failed"
    CANCELLED = "cancelled"


class PackageReceiptDisposition(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class RejectionDisposition(StrEnum):
    RETRYABLE_WAIT = "retryable_wait"
    BLOCKED_WAIT = "blocked_wait"
    PERMANENT_FAILURE = "permanent_failure"


class CommandKind(StrEnum):
    TASK_PACKAGE_AVAILABLE = "task_package_available"
    CANCEL_REQUESTED = "cancel_requested"


class CommandStatus(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    SUPERSEDED = "superseded"


class CancellationAcknowledgementOutcome(StrEnum):
    RUNNING_CANCEL_ACCEPTED = "running_cancel_accepted"
    ALREADY_COMPLETED = "already_completed"
    CANCELLED_BEFORE_START = "cancelled_before_start"


class TerminalReportKind(StrEnum):
    PACKAGE_RECEIPT = "package_receipt"
    ATTEMPT_START = "attempt_start"
    ATTEMPT_RESULT = "attempt_result"


class TerminalReportDisposition(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    REJECTED = "rejected"


class TerminalResultKind(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class PackageResource:
    blob_id: UUID
    resource_key: str
    role: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class TaskPackageRecord:
    package_id: UUID
    attempt_id: UUID
    execution_id: UUID
    snapshot_id: UUID
    terminal_id: UUID
    target_device_id: UUID | None
    protocol_version: int
    package_schema_version: int
    snapshot_schema_version: int
    package_hash: str
    manifest: dict[str, Any]
    status: PackageStatus
    delivery_attempt_count: int
    next_delivery_at: datetime
    last_rejection_disposition: RejectionDisposition | None
    last_rejection_code: str | None
    last_rejection_diagnostic: str | None
    accepted_at: datetime | None
    failed_at: datetime | None
    created_at: datetime
    row_version: int


@dataclass(frozen=True, slots=True)
class TerminalCommandRecord:
    command_id: UUID
    terminal_id: UUID
    command_kind: CommandKind
    package_id: UUID | None
    attempt_id: UUID
    delivery_no: int
    status: CommandStatus
    payload: dict[str, Any]
    available_at: datetime
    acknowledged_at: datetime | None
    created_at: datetime
    row_version: int


@dataclass(frozen=True, slots=True)
class CommandAcknowledgementRecord:
    terminal_id: UUID
    report_id: UUID
    command_id: UUID
    attempt_id: UUID
    execution_id: UUID
    outcome: CancellationAcknowledgementOutcome
    payload_hash: str
    occurred_at: datetime
    received_at: datetime


@dataclass(frozen=True, slots=True)
class CommandAcknowledgementResult:
    command: TerminalCommandRecord
    execution: ExecutionRecord
    acknowledgement: CommandAcknowledgementRecord


@dataclass(frozen=True, slots=True)
class TerminalReportRecord:
    terminal_id: UUID
    report_id: UUID
    report_kind: TerminalReportKind
    disposition: TerminalReportDisposition
    command_id: UUID | None
    attempt_id: UUID
    package_id: UUID | None
    lease_id: UUID | None
    error_code: str | None
    diagnostic: str | None
    payload_hash: str
    occurred_at: datetime
    received_at: datetime
    result_diagnostic: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PackageReceiptResult:
    package: TaskPackageRecord
    execution: ExecutionRecord
    report: TerminalReportRecord
    command: TerminalCommandRecord | None
    offline_start_permit: OfflineStartPermitGrant | None


@dataclass(frozen=True, slots=True)
class PackageRedeliveryResult:
    package: TaskPackageRecord
    execution: ExecutionRecord
    command: TerminalCommandRecord


@dataclass(frozen=True, slots=True)
class AttemptStartResult:
    execution: ExecutionRecord
    attempt: ExecutionAttemptRecord
    lease: ExecutionLeaseRecord
    report: TerminalReportRecord


@dataclass(frozen=True, slots=True)
class TerminalResultReceipt:
    execution: ExecutionRecord
    attempt: ExecutionAttemptRecord
    report: TerminalReportRecord
    retry_attempt: ExecutionAttemptRecord | None


@dataclass(frozen=True, slots=True)
class PrestartFailureResult:
    execution: ExecutionRecord
    attempt: ExecutionAttemptRecord
    report: TerminalReportRecord


RETRYABLE_REJECTION_CODES = frozenset(
    {
        "PACKAGE_DOWNLOAD_TIMEOUT",
        "PACKAGE_DOWNLOAD_INTERRUPTED",
        "RESOURCE_HASH_MISMATCH",
        "NETWORK_UNSTABLE",
    }
)

BLOCKED_REJECTION_CODES = frozenset(
    {
        "TARGET_DEVICE_UNAVAILABLE",
        "TERMINAL_RESOURCE_BUSY",
        "INSUFFICIENT_STORAGE",
        "TEMPORARY_STORAGE_UNAVAILABLE",
    }
)

PERMANENT_REJECTION_CODES = frozenset(
    {
        "PROTOCOL_VERSION_UNSUPPORTED",
        "PACKAGE_SCHEMA_UNSUPPORTED",
        "SNAPSHOT_SCHEMA_UNSUPPORTED",
        "PACKAGE_MANIFEST_INVALID",
        "CAPABILITY_MISMATCH",
        "RESOURCE_VARIANT_UNSUPPORTED",
        "REQUIRED_RESOURCE_MISSING",
    }
)


def classify_rejection(code: str) -> RejectionDisposition:
    if code in RETRYABLE_REJECTION_CODES:
        return RejectionDisposition.RETRYABLE_WAIT
    if code in BLOCKED_REJECTION_CODES:
        return RejectionDisposition.BLOCKED_WAIT
    if code in PERMANENT_REJECTION_CODES:
        return RejectionDisposition.PERMANENT_FAILURE
    raise ValueError(f"unknown terminal rejection code: {code}")
