from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class TaskRequestRow(Base):
    __tablename__ = "task_requests"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('single', 'loop', 'timed', 'batch')", name="ck_task_requests_type"
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'completed', 'cancelled', 'terminated')",
            name="ck_task_requests_status",
        ),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_task_requests_hash"),
        CheckConstraint("timeout_seconds > 0", name="ck_task_requests_timeout"),
        CheckConstraint("max_retries >= 0 AND max_retries <= 100", name="ck_task_requests_retries"),
        CheckConstraint("row_version > 0", name="ck_task_requests_version"),
        UniqueConstraint("idempotency_key", name="uq_task_requests_idempotency"),
        Index(
            "ix_task_requests_history",
            "lifecycle_status",
            text("created_at DESC"),
            "id",
        ),
        Index("ix_task_requests_target", "requested_terminal_id", "lifecycle_status"),
        Index(
            "ix_task_requests_content_schedule",
            "source_module",
            "logical_content_id",
            "lifecycle_status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    source_module: Mapped[str] = mapped_column(String(80), nullable=False)
    logical_content_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requested_terminal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT")
    )
    requested_target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False)
    record_video: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TaskScheduleRow(Base):
    __tablename__ = "task_schedules"
    __table_args__ = (
        CheckConstraint("schedule_type IN ('loop', 'timed')", name="ck_task_schedules_type"),
        CheckConstraint(
            "status IN ('active', 'paused', 'terminating', 'terminated', 'completed')",
            name="ck_task_schedules_status",
        ),
        CheckConstraint(
            "total_occurrences > 0 AND total_occurrences <= 10000",
            name="ck_task_schedules_total",
        ),
        CheckConstraint("row_version > 0", name="ck_task_schedules_version"),
        CheckConstraint(
            "(schedule_type = 'loop' AND current_revision IS NULL) OR "
            "(schedule_type = 'timed' AND current_revision > 0)",
            name="ck_task_schedules_current_revision",
        ),
        CheckConstraint(
            "(schedule_type = 'loop' AND repeat_count = total_occurrences "
            "AND timezone IS NULL AND start_date IS NULL AND end_date IS NULL) OR "
            "(schedule_type = 'timed' AND repeat_count IS NULL AND timezone IS NOT NULL "
            "AND start_date IS NOT NULL AND end_date IS NOT NULL AND end_date >= start_date)",
            name="ck_task_schedules_definition",
        ),
        UniqueConstraint("task_request_id", name="uq_task_schedules_request"),
        Index("ix_task_schedules_active", "status", "updated_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    task_request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    total_occurrences: Mapped[int] = mapped_column(Integer, nullable=False)
    repeat_count: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str | None] = mapped_column(String(100))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    current_revision: Mapped[int | None] = mapped_column(Integer)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TaskScheduleRevisionRow(Base):
    __tablename__ = "task_schedule_revisions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_task_schedule_revisions_revision"),
        CheckConstraint("end_date >= start_date", name="ck_task_schedule_revisions_dates"),
        UniqueConstraint("schedule_id", "revision", name="uq_task_schedule_revisions_number"),
        Index("ix_task_schedule_revisions_schedule", "schedule_id", "revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    schedule_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class TaskScheduleRevisionTimeRow(Base):
    __tablename__ = "task_schedule_revision_times"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_task_schedule_revision_times_ordinal"),
        UniqueConstraint(
            "schedule_revision_id", "ordinal", name="uq_task_schedule_revision_times_ordinal"
        ),
        UniqueConstraint(
            "schedule_revision_id", "local_time", name="uq_task_schedule_revision_times_value"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    schedule_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_schedule_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    local_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)


class PlanOccurrenceRow(Base):
    __tablename__ = "plan_occurrences"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_plan_occurrences_ordinal"),
        CheckConstraint(
            "status IN ('planned', 'materialized', 'skipped', 'cancelled')",
            name="ck_plan_occurrences_status",
        ),
        CheckConstraint(
            "(materialization_owner IS NULL AND materialization_expires_at IS NULL) OR "
            "(materialization_owner IS NOT NULL AND materialization_expires_at IS NOT NULL)",
            name="ck_plan_occurrences_claim",
        ),
        CheckConstraint("row_version > 0", name="ck_plan_occurrences_version"),
        UniqueConstraint("task_request_id", "ordinal", name="uq_plan_occurrences_request_ordinal"),
        Index(
            "uq_plan_occurrences_request_time",
            "schedule_id",
            "scheduled_for",
            unique=True,
            postgresql_where=text(
                "scheduled_for IS NOT NULL AND status IN ('planned', 'materialized')"
            ),
        ),
        Index(
            "ix_plan_occurrences_due",
            "status",
            "scheduled_for",
            "materialization_expires_at",
            "ordinal",
            "id",
        ),
        Index("ix_plan_occurrences_schedule", "schedule_id", "ordinal"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    task_request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    schedule_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_schedules.id", ondelete="RESTRICT")
    )
    schedule_revision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_schedule_revisions.id", ondelete="RESTRICT")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    materialization_owner: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    materialization_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExecutionRow(Base):
    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting', 'queued', 'running', 'ended', 'cancelled', 'timed_out')",
            name="ck_executions_status",
        ),
        CheckConstraint(
            "(status = 'ended' AND result IN ('success', 'failure')) OR "
            "(status <> 'ended' AND result IS NULL)",
            name="ck_executions_result",
        ),
        CheckConstraint(
            "timeout_at IS NULL OR timeout_at > created_at",
            name="ck_executions_timeout",
        ),
        CheckConstraint(
            "cancel_reason IS NULL OR cancel_reason IN ('operator', 'timeout')",
            name="ck_executions_cancel_reason",
        ),
        CheckConstraint(
            "(cancel_requested_at IS NULL AND cancel_reason IS NULL "
            "AND cancel_deadline_at IS NULL) OR "
            "(cancel_requested_at IS NOT NULL AND cancel_reason IS NOT NULL "
            "AND (cancel_deadline_at IS NULL OR cancel_deadline_at > cancel_requested_at))",
            name="ck_executions_cancel_window",
        ),
        CheckConstraint(
            "(cancel_delivery_delayed = false AND cancel_delivery_note IS NULL) OR "
            "(cancel_delivery_delayed = true AND cancel_delivery_note IS NOT NULL)",
            name="ck_executions_cancel_delivery",
        ),
        CheckConstraint("row_version > 0", name="ck_executions_version"),
        UniqueConstraint("occurrence_id", name="uq_executions_occurrence"),
        Index("ix_executions_task", "task_request_id", "created_at"),
        Index("ix_executions_target_status", "target_device_id", "status"),
        Index("ix_executions_terminal_status", "terminal_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    task_request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_requests.id", ondelete="RESTRICT"), nullable=False
    )
    occurrence_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plan_occurrences.id", ondelete="RESTRICT"),
        nullable=False,
    )
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str | None] = mapped_column(String(32))
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(32))
    cancel_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_delivery_delayed: Mapped[bool] = mapped_column(nullable=False, default=False)
    cancel_delivery_note: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExecutionAttemptRow(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        CheckConstraint("attempt_no > 0", name="ck_execution_attempts_number"),
        CheckConstraint(
            "status IN ('queued', 'running', 'ended', 'cancelled', 'timed_out')",
            name="ck_execution_attempts_status",
        ),
        CheckConstraint(
            "(status = 'ended' AND result IN ('success', 'failure')) OR "
            "(status <> 'ended' AND result IS NULL)",
            name="ck_execution_attempts_result",
        ),
        CheckConstraint(
            "failure_phase IS NULL OR "
            "failure_phase IN ('delivery', 'runtime', 'cleanup', 'platform')",
            name="ck_execution_attempts_failure_phase",
        ),
        CheckConstraint("row_version > 0", name="ck_execution_attempts_version"),
        UniqueConstraint("execution_id", "attempt_no", name="uq_execution_attempts_number"),
        Index(
            "ix_execution_attempts_fifo",
            "status",
            "available_at",
            "enqueued_at",
            "id",
        ),
        Index(
            "uq_execution_attempts_active",
            "execution_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str | None] = mapped_column(String(32))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    failure_phase: Mapped[str | None] = mapped_column(String(32))
    lease_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_leases.id", ondelete="RESTRICT")
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExecutionSnapshotRow(Base):
    __tablename__ = "execution_snapshots"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_execution_snapshots_schema"),
        CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_execution_snapshots_hash"),
        CheckConstraint("timeout_seconds > 0", name="ck_execution_snapshots_timeout"),
        CheckConstraint(
            "max_retries >= 0 AND max_retries <= 100",
            name="ck_execution_snapshots_retries",
        ),
        UniqueConstraint("execution_id", name="uq_execution_snapshots_execution"),
        Index("ix_execution_snapshots_source", "source_module", "logical_content_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
    )
    source_module: Mapped[str] = mapped_column(String(80), nullable=False)
    logical_content_id: Mapped[str] = mapped_column(String(255), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    capability_requirements: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False)
    record_video: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class ExecutionSnapshotBlobRow(Base):
    __tablename__ = "execution_snapshot_blobs"
    __table_args__ = (
        PrimaryKeyConstraint("snapshot_id", "resource_key"),
        UniqueConstraint("snapshot_id", "blob_id", "role", name="uq_snapshot_blobs_role"),
        Index("ix_execution_snapshot_blobs_blob", "blob_id"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    resource_key: Mapped[str] = mapped_column(String(255), nullable=False)
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blob_objects.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(80), nullable=False)


class TaskRetryOriginRow(Base):
    __tablename__ = "task_retry_origins"
    __table_args__ = (Index("ix_task_retry_origins_source", "source_execution_id"),)

    task_request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_requests.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
    )
    source_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class StateTransitionRow(Base):
    __tablename__ = "state_transitions"
    __table_args__ = (
        CheckConstraint(
            "aggregate_type IN ('task_request', 'task_schedule', 'plan_occurrence', "
            "'execution', 'execution_attempt')",
            name="ck_state_transitions_aggregate",
        ),
        Index(
            "ix_state_transitions_aggregate",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
            "id",
        ),
        Index("ix_state_transitions_correlation", "correlation_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
