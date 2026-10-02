from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import TaskPackageRow
from al1s.adapters.postgres.execution_models import (
    ExecutionLeaseRow,
    TargetDeviceRow,
    TerminalCapabilityProfileRow,
    TerminalRow,
)
from al1s.adapters.postgres.scheduling_models import (
    ExecutionAttemptRow,
    ExecutionRow,
    PlanOccurrenceRow,
    TaskRequestRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    MAX_PAGE_SIZE,
    MAX_WORKER_BATCH,
    _attempt_record,
    _execution_record,
)
from al1s.adapters.postgres.target_control import lock_target_assignment
from al1s.execution.scheduling_types import (
    AttemptStatus,
    CleanupDeadlineCandidate,
    EligibleExecutionResource,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionStatus,
    RuntimeTimeoutCandidate,
)
from al1s.execution.types import CapabilityManifest


class PostgresExecutionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def maintenance_impact(self, terminal_id: UUID) -> list[dict[str, str]]:
        rows = self._session.execute(
            select(
                ExecutionRow.id,
                ExecutionRow.task_request_id,
                TaskRequestRow.name,
                ExecutionRow.status,
            )
            .join(TaskRequestRow, TaskRequestRow.id == ExecutionRow.task_request_id)
            .where(
                ExecutionRow.terminal_id == terminal_id,
                ExecutionRow.status.in_(("waiting", "queued", "running")),
            )
            .order_by(ExecutionRow.created_at, ExecutionRow.id)
            .limit(101)
        ).all()
        return [
            {
                "execution_id": str(row.id),
                "task_request_id": str(row.task_request_id),
                "name": row.name,
                "status": row.status,
            }
            for row in rows
        ]

    def list_eligible_resources(
        self,
        *,
        requested_terminal_ids: set[UUID],
        requested_target_device_ids: set[UUID],
        include_unrestricted: bool,
        limit: int,
    ) -> list[EligibleExecutionResource]:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        target_rows = self._session.execute(
            select(TargetDeviceRow.id, TargetDeviceRow.managing_terminal_id).where(
                TargetDeviceRow.deleted_at.is_(None),
                TargetDeviceRow.managing_terminal_id.is_not(None),
                or_(
                    TargetDeviceRow.id.in_(requested_target_device_ids),
                    TargetDeviceRow.managing_terminal_id.in_(requested_terminal_ids),
                ),
            )
        ).all()
        terminal_ids = set(requested_terminal_ids)
        terminal_ids.update(row.managing_terminal_id for row in target_rows)
        statement = (
            select(TerminalRow, TerminalCapabilityProfileRow)
            .join(
                TerminalCapabilityProfileRow,
                TerminalCapabilityProfileRow.id == TerminalRow.current_capability_profile_id,
            )
            .where(
                TerminalRow.deleted_at.is_(None),
                TerminalRow.service_status == "online",
                TerminalRow.acceptance_status == "accepting",
            )
            .order_by(TerminalRow.id)
            .limit(limit)
        )
        if not include_unrestricted:
            if not terminal_ids:
                return []
            statement = statement.where(TerminalRow.id.in_(terminal_ids))
        rows = self._session.execute(statement).all()
        selected_terminal_ids = {terminal.id for terminal, _ in rows}
        managed_rows = self._session.execute(
            select(TargetDeviceRow.id, TargetDeviceRow.managing_terminal_id).where(
                TargetDeviceRow.deleted_at.is_(None),
                TargetDeviceRow.managing_terminal_id.in_(selected_terminal_ids),
            )
        ).all()
        targets_by_terminal: defaultdict[UUID, list[UUID]] = defaultdict(list)
        for row in managed_rows:
            targets_by_terminal[row.managing_terminal_id].append(row.id)
        return [
            EligibleExecutionResource(
                terminal_id=terminal.id,
                capability=CapabilityManifest(
                    schema_version=profile.schema_version,
                    protocol_version=profile.protocol_version,
                    agent_version=profile.agent_version,
                    os_name=profile.os_name,
                    os_version=profile.os_version,
                    architecture=profile.architecture,
                    cpu_cores=profile.cpu_cores,
                    memory_bytes=profile.memory_bytes,
                    storage_available_bytes=profile.storage_available_bytes,
                    accelerator_type=profile.accelerator_type,
                    low_resource=profile.low_resource,
                    provider_keys=tuple(profile.provider_keys),
                    details=dict(profile.details),
                ),
                managed_target_device_ids=tuple(sorted(targets_by_terminal[terminal.id], key=str)),
            )
            for terminal, profile in rows
        ]

    def add(self, execution: ExecutionRecord) -> None:
        lock_target_assignment(self._session, execution.target_device_id, execution.terminal_id)
        self._session.add(
            ExecutionRow(
                id=execution.execution_id,
                task_request_id=execution.task_request_id,
                occurrence_id=execution.occurrence_id,
                terminal_id=execution.terminal_id,
                target_device_id=execution.target_device_id,
                status=execution.status.value,
                result=execution.result.value if execution.result else None,
                timeout_at=execution.timeout_at,
                cancel_requested_at=execution.cancel_requested_at,
                cancel_reason=execution.cancel_reason,
                cancel_deadline_at=execution.cancel_deadline_at,
                created_at=execution.created_at,
                queued_at=execution.queued_at,
                started_at=execution.started_at,
                ended_at=execution.ended_at,
                row_version=execution.row_version,
            )
        )

    def get_for_update(self, execution_id: UUID) -> ExecutionRecord | None:
        row = self._session.scalar(
            select(ExecutionRow).where(ExecutionRow.id == execution_id).with_for_update()
        )
        return _execution_record(row) if row else None

    def get(self, execution_id: UUID) -> ExecutionRecord | None:
        row = self._session.get(ExecutionRow, execution_id)
        return _execution_record(row) if row else None

    def get_by_task_for_update(self, task_id: UUID) -> ExecutionRecord | None:
        row = self._session.scalar(
            select(ExecutionRow)
            .where(ExecutionRow.task_request_id == task_id)
            .order_by(ExecutionRow.created_at.desc(), ExecutionRow.id.desc())
            .limit(1)
            .with_for_update()
        )
        return _execution_record(row) if row else None

    def get_by_task(self, task_id: UUID) -> ExecutionRecord | None:
        row = self._session.scalar(
            select(ExecutionRow)
            .where(ExecutionRow.task_request_id == task_id)
            .order_by(ExecutionRow.created_at.desc(), ExecutionRow.id.desc())
            .limit(1)
        )
        return _execution_record(row) if row else None

    def request_cancel(
        self,
        execution_id: UUID,
        expected_version: int,
        now: datetime,
        *,
        reason: str,
        deadline_at: datetime | None,
    ) -> ExecutionRecord | None:
        row = self._session.execute(
            update(ExecutionRow)
            .where(
                ExecutionRow.id == execution_id,
                ExecutionRow.row_version == expected_version,
                ExecutionRow.status.in_(
                    (ExecutionStatus.QUEUED.value, ExecutionStatus.RUNNING.value)
                ),
                ExecutionRow.cancel_requested_at.is_(None),
            )
            .values(
                cancel_requested_at=now,
                cancel_reason=reason,
                cancel_deadline_at=deadline_at,
                row_version=ExecutionRow.row_version + 1,
            )
            .returning(ExecutionRow)
        ).scalar_one_or_none()
        return _execution_record(row) if row else None

    def acknowledge_cancel_delivery(
        self,
        execution_id: UUID,
        expected_version: int,
        *,
        deadline_at: datetime | None,
        delayed: bool,
        note: str | None,
    ) -> ExecutionRecord | None:
        row = self._session.execute(
            update(ExecutionRow)
            .where(
                ExecutionRow.id == execution_id,
                ExecutionRow.row_version == expected_version,
                ExecutionRow.cancel_requested_at.is_not(None),
            )
            .values(
                cancel_deadline_at=deadline_at,
                cancel_delivery_delayed=delayed,
                cancel_delivery_note=note,
                row_version=ExecutionRow.row_version + 1,
            )
            .returning(ExecutionRow)
        ).scalar_one_or_none()
        return _execution_record(row) if row else None

    def touch_waiting(self, execution_id: UUID, expected_version: int) -> ExecutionRecord | None:
        row = self._session.execute(
            update(ExecutionRow)
            .where(
                ExecutionRow.id == execution_id,
                ExecutionRow.row_version == expected_version,
                ExecutionRow.status == ExecutionStatus.WAITING.value,
            )
            .values(row_version=ExecutionRow.row_version + 1)
            .returning(ExecutionRow)
        ).scalar_one_or_none()
        return _execution_record(row) if row else None

    def set_status(
        self,
        execution_id: UUID,
        expected_version: int,
        status: str,
        *,
        result: str | None,
        now: datetime,
        timeout_at: datetime | None,
    ) -> ExecutionRecord | None:
        values: dict[str, object] = {
            "status": status,
            "result": result,
            "row_version": ExecutionRow.row_version + 1,
        }
        if status == ExecutionStatus.RUNNING.value:
            values.update(started_at=now, timeout_at=timeout_at)
        elif status in {ExecutionStatus.WAITING.value, ExecutionStatus.QUEUED.value}:
            values.update(queued_at=now, timeout_at=None)
        elif status in {
            ExecutionStatus.ENDED.value,
            ExecutionStatus.CANCELLED.value,
            ExecutionStatus.TIMED_OUT.value,
        }:
            values["ended_at"] = now
        row = self._session.execute(
            update(ExecutionRow)
            .where(
                ExecutionRow.id == execution_id,
                ExecutionRow.row_version == expected_version,
            )
            .values(**values)
            .returning(ExecutionRow)
        ).scalar_one_or_none()
        return _execution_record(row) if row else None

    def cancel_not_running(
        self, schedule_id: UUID, now: datetime
    ) -> tuple[list[ExecutionRecord], list[ExecutionAttemptRecord]]:
        execution_ids = (
            select(ExecutionRow.id)
            .join(PlanOccurrenceRow, PlanOccurrenceRow.id == ExecutionRow.occurrence_id)
            .where(
                PlanOccurrenceRow.schedule_id == schedule_id,
                ExecutionRow.status.in_(
                    (ExecutionStatus.WAITING.value, ExecutionStatus.QUEUED.value)
                ),
            )
        )
        attempt_rows = (
            self._session.execute(
                update(ExecutionAttemptRow)
                .where(
                    ExecutionAttemptRow.execution_id.in_(execution_ids),
                    ExecutionAttemptRow.status == AttemptStatus.QUEUED.value,
                )
                .values(
                    status=AttemptStatus.CANCELLED.value,
                    ended_at=now,
                    row_version=ExecutionAttemptRow.row_version + 1,
                )
                .returning(ExecutionAttemptRow)
            )
            .scalars()
            .all()
        )
        execution_rows = (
            self._session.execute(
                update(ExecutionRow)
                .where(ExecutionRow.id.in_(execution_ids))
                .values(
                    status=ExecutionStatus.CANCELLED.value,
                    cancel_requested_at=now,
                    cancel_reason="operator",
                    ended_at=now,
                    row_version=ExecutionRow.row_version + 1,
                )
                .returning(ExecutionRow)
            )
            .scalars()
            .all()
        )
        return (
            [_execution_record(row) for row in execution_rows],
            [_attempt_record(row) for row in attempt_rows],
        )

    def has_running(self, schedule_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(ExecutionRow.id)
                .join(PlanOccurrenceRow, PlanOccurrenceRow.id == ExecutionRow.occurrence_id)
                .where(
                    PlanOccurrenceRow.schedule_id == schedule_id,
                    ExecutionRow.status == ExecutionStatus.RUNNING.value,
                )
                .limit(1)
            )
            is not None
        )

    def list_runtime_timeouts_for_update(
        self, *, now: datetime, limit: int
    ) -> list[RuntimeTimeoutCandidate]:
        if not 1 <= limit <= MAX_WORKER_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_WORKER_BATCH}")
        rows = self._session.execute(
            select(
                ExecutionRow,
                ExecutionAttemptRow.id,
                TaskPackageRow.id,
                TaskPackageRow.protocol_version,
            )
            .join(
                ExecutionAttemptRow,
                and_(
                    ExecutionAttemptRow.execution_id == ExecutionRow.id,
                    ExecutionAttemptRow.status == AttemptStatus.RUNNING.value,
                ),
            )
            .join(TaskPackageRow, TaskPackageRow.attempt_id == ExecutionAttemptRow.id)
            .where(
                ExecutionRow.status == ExecutionStatus.RUNNING.value,
                ExecutionRow.timeout_at <= now,
                ExecutionRow.cancel_requested_at.is_(None),
            )
            .order_by(ExecutionRow.timeout_at, ExecutionRow.id)
            .limit(limit)
            .with_for_update(of=ExecutionRow, skip_locked=True)
        ).all()
        return [
            RuntimeTimeoutCandidate(
                execution=_execution_record(execution),
                attempt_id=attempt_id,
                package_id=package_id,
                protocol_version=protocol_version,
            )
            for execution, attempt_id, package_id, protocol_version in rows
        ]

    def list_cleanup_deadlines_for_update(
        self, *, now: datetime, limit: int
    ) -> list[CleanupDeadlineCandidate]:
        if not 1 <= limit <= MAX_WORKER_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_WORKER_BATCH}")
        rows = self._session.execute(
            select(
                ExecutionRow,
                ExecutionAttemptRow,
                ExecutionLeaseRow.id,
                ExecutionLeaseRow.row_version,
            )
            .join(
                ExecutionAttemptRow,
                and_(
                    ExecutionAttemptRow.execution_id == ExecutionRow.id,
                    ExecutionAttemptRow.status == AttemptStatus.RUNNING.value,
                ),
            )
            .outerjoin(
                ExecutionLeaseRow,
                and_(
                    ExecutionLeaseRow.id == ExecutionAttemptRow.lease_id,
                    ExecutionLeaseRow.owner_kind == "execution_attempt",
                    ExecutionLeaseRow.owner_id == ExecutionAttemptRow.id,
                    ExecutionLeaseRow.released_at.is_(None),
                ),
            )
            .where(
                ExecutionRow.status == ExecutionStatus.RUNNING.value,
                ExecutionRow.cancel_deadline_at <= now,
            )
            .order_by(ExecutionRow.cancel_deadline_at, ExecutionRow.id)
            .limit(limit)
            .with_for_update(of=ExecutionRow, skip_locked=True)
        ).all()
        return [
            CleanupDeadlineCandidate(
                execution=_execution_record(execution),
                attempt=_attempt_record(attempt),
                lease_id=lease_id,
                lease_version=lease_version,
            )
            for execution, attempt, lease_id, lease_version in rows
        ]
