from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid4

from al1s.execution.delivery_types import (
    CommandKind,
    CommandStatus,
    PackageResource,
    PackageStatus,
    TaskPackageRecord,
    TerminalCommandRecord,
)
from al1s.execution.scheduling_types import (
    ExecutionAttemptRecord,
    ExecutionSnapshotRecord,
)


def build_task_package_manifest(
    *,
    package_id: UUID,
    attempt: ExecutionAttemptRecord,
    snapshot: ExecutionSnapshotRecord,
    resources: tuple[PackageResource, ...],
    created_at: datetime,
) -> tuple[dict[str, object], str]:
    """Build the immutable, secret-free Stage 3C package body and its canonical hash."""
    body: dict[str, object] = {
        "protocol_version": 1,
        "package_schema_version": 1,
        "snapshot_schema_version": snapshot.schema_version,
        "package_id": str(package_id),
        "execution_id": str(snapshot.execution_id),
        "attempt_id": str(attempt.attempt_id),
        "queue_order": {
            "available_at": attempt.available_at.isoformat(),
            "enqueued_at": attempt.enqueued_at.isoformat(),
        },
        "snapshot_id": str(snapshot.snapshot_id),
        "terminal_id": str(snapshot.terminal_id),
        "target_device_id": (str(snapshot.target_device_id) if snapshot.target_device_id else None),
        "created_at": created_at.isoformat(),
        "source": {
            "module": snapshot.source_module,
            "logical_content_id": snapshot.logical_content_id,
            "revision_id": snapshot.revision_id,
            "manifest_hash": snapshot.manifest_hash,
        },
        "manifest": dict(snapshot.manifest),
        "resources": [
            {
                "blob_id": str(resource.blob_id),
                "resource_key": resource.resource_key,
                "role": resource.role,
                "sha256": resource.sha256,
                "size": resource.size_bytes,
                "media_type": resource.media_type,
            }
            for resource in resources
        ],
        "parameters": dict(snapshot.parameters),
        "capability_requirements": asdict(snapshot.capability_requirements),
        "timeout_seconds": snapshot.timeout_seconds,
        "attempt_no": attempt.attempt_no,
        "max_retries": snapshot.max_retries,
        "record_video": snapshot.record_video,
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return body, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_delivery_records(
    *,
    attempt: ExecutionAttemptRecord,
    snapshot: ExecutionSnapshotRecord,
    resources: tuple[PackageResource, ...],
    created_at: datetime,
) -> tuple[TaskPackageRecord, TerminalCommandRecord]:
    package_id = uuid4()
    body, package_hash = build_task_package_manifest(
        package_id=package_id,
        attempt=attempt,
        snapshot=snapshot,
        resources=resources,
        created_at=created_at,
    )
    package = TaskPackageRecord(
        package_id=package_id,
        attempt_id=attempt.attempt_id,
        execution_id=snapshot.execution_id,
        snapshot_id=snapshot.snapshot_id,
        terminal_id=snapshot.terminal_id,
        target_device_id=snapshot.target_device_id,
        protocol_version=1,
        package_schema_version=1,
        snapshot_schema_version=snapshot.schema_version,
        package_hash=package_hash,
        manifest=body,
        status=PackageStatus.AVAILABLE,
        delivery_attempt_count=1,
        next_delivery_at=created_at,
        last_rejection_disposition=None,
        last_rejection_code=None,
        last_rejection_diagnostic=None,
        accepted_at=None,
        failed_at=None,
        created_at=created_at,
        row_version=1,
    )
    command = TerminalCommandRecord(
        command_id=uuid4(),
        terminal_id=snapshot.terminal_id,
        command_kind=CommandKind.TASK_PACKAGE_AVAILABLE,
        package_id=package_id,
        attempt_id=attempt.attempt_id,
        delivery_no=1,
        status=CommandStatus.PENDING,
        payload={
            "protocol_version": 1,
            "package_id": str(package_id),
            "package_hash": package_hash,
        },
        available_at=created_at,
        acknowledged_at=None,
        created_at=created_at,
        row_version=1,
    )
    return package, command
