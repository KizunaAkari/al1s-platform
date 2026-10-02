from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from al1s.execution.package_builder import build_task_package_manifest
from al1s.execution.scheduling_types import (
    CapabilityRequirements,
    ExecutionAttemptRecord,
    ExecutionSnapshotRecord,
)


def test_package_queue_key_uses_attempt_not_delivery_time_and_is_hashed() -> None:
    enqueued = datetime(2026, 9, 7, tzinfo=UTC)
    attempt = SimpleNamespace(
        attempt_id=uuid4(),
        available_at=enqueued,
        enqueued_at=enqueued,
        attempt_no=1,
    )
    snapshot = SimpleNamespace(
        schema_version=1,
        execution_id=uuid4(),
        snapshot_id=uuid4(),
        terminal_id=uuid4(),
        target_device_id=None,
        source_module="maa",
        logical_content_id="script:test",
        revision_id="v1",
        manifest_hash="a" * 64,
        manifest={},
        parameters={},
        capability_requirements=CapabilityRequirements(),
        timeout_seconds=60,
        max_retries=0,
        record_video=False,
    )
    package_id = uuid4()

    def build() -> tuple[dict[str, object], str]:
        return build_task_package_manifest(
            package_id=package_id,
            attempt=cast(ExecutionAttemptRecord, attempt),
            snapshot=cast(ExecutionSnapshotRecord, snapshot),
            resources=(),
            created_at=enqueued + timedelta(minutes=2),
        )

    body, first_hash = build()
    assert body["queue_order"] == {
        "available_at": enqueued.isoformat(),
        "enqueued_at": enqueued.isoformat(),
    }
    assert body["attempt_id"] == str(attempt.attempt_id)
    attempt.available_at += timedelta(seconds=1)
    assert build()[1] != first_hash
