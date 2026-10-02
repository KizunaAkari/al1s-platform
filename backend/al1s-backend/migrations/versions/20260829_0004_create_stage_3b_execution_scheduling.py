"""create stage 3b execution scheduling

Revision ID: 20260829_0004
Revises: 20260829_0003
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0004"
down_revision: str | None = "20260829_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "state_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=40), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=40), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "aggregate_type IN ('task_request', 'task_schedule', 'plan_occurrence', "
            "'execution', 'execution_attempt')",
            name="ck_state_transitions_aggregate",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_state_transitions_aggregate",
        "state_transitions",
        ["aggregate_type", "aggregate_id", "occurred_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_state_transitions_correlation",
        "state_transitions",
        ["correlation_id", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "task_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("source_module", sa.String(length=80), nullable=False),
        sa.Column("logical_content_id", sa.String(length=255), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requested_terminal_id", sa.Uuid(), nullable=True),
        sa.Column("requested_target_device_id", sa.Uuid(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("record_video", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'completed', 'cancelled', 'terminated')",
            name="ck_task_requests_status",
        ),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_task_requests_hash"),
        sa.CheckConstraint(
            "task_type IN ('single', 'loop', 'timed')", name="ck_task_requests_type"
        ),
        sa.CheckConstraint(
            "max_retries >= 0 AND max_retries <= 100", name="ck_task_requests_retries"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_task_requests_version"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_task_requests_timeout"),
        sa.ForeignKeyConstraint(
            ["requested_target_device_id"], ["target_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["requested_terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_task_requests_idempotency"),
    )
    op.create_index(
        "ix_task_requests_history",
        "task_requests",
        ["lifecycle_status", sa.literal_column("created_at DESC"), "id"],
        unique=False,
    )
    op.create_index(
        "ix_task_requests_target",
        "task_requests",
        ["requested_terminal_id", "lifecycle_status"],
        unique=False,
    )
    op.create_table(
        "task_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_request_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_occurrences", sa.Integer(), nullable=False),
        sa.Column("repeat_count", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(length=100), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(schedule_type = 'loop' AND repeat_count = total_occurrences "
            "AND timezone IS NULL AND start_date IS NULL AND end_date IS NULL) OR "
            "(schedule_type = 'timed' AND repeat_count IS NULL "
            "AND timezone IS NOT NULL AND start_date IS NOT NULL "
            "AND end_date IS NOT NULL AND end_date >= start_date)",
            name="ck_task_schedules_definition",
        ),
        sa.CheckConstraint("schedule_type IN ('loop', 'timed')", name="ck_task_schedules_type"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'terminating', 'terminated', 'completed')",
            name="ck_task_schedules_status",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_task_schedules_version"),
        sa.CheckConstraint(
            "(schedule_type = 'loop' AND current_revision IS NULL) OR "
            "(schedule_type = 'timed' AND current_revision > 0)",
            name="ck_task_schedules_current_revision",
        ),
        sa.CheckConstraint(
            "total_occurrences > 0 AND total_occurrences <= 10000", name="ck_task_schedules_total"
        ),
        sa.ForeignKeyConstraint(["task_request_id"], ["task_requests.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_request_id", name="uq_task_schedules_request"),
    )
    op.create_index(
        "ix_task_schedules_active", "task_schedules", ["status", "updated_at", "id"], unique=False
    )
    op.create_table(
        "task_schedule_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("revision > 0", name="ck_task_schedule_revisions_revision"),
        sa.CheckConstraint("end_date >= start_date", name="ck_task_schedule_revisions_dates"),
        sa.ForeignKeyConstraint(["schedule_id"], ["task_schedules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "revision", name="uq_task_schedule_revisions_number"
        ),
    )
    op.create_index(
        "ix_task_schedule_revisions_schedule",
        "task_schedule_revisions",
        ["schedule_id", "revision"],
        unique=False,
    )
    op.create_table(
        "task_schedule_revision_times",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schedule_revision_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("local_time", sa.Time(), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_task_schedule_revision_times_ordinal"),
        sa.ForeignKeyConstraint(
            ["schedule_revision_id"], ["task_schedule_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_revision_id", "local_time", name="uq_task_schedule_revision_times_value"
        ),
        sa.UniqueConstraint(
            "schedule_revision_id", "ordinal", name="uq_task_schedule_revision_times_ordinal"
        ),
    )
    op.create_table(
        "plan_occurrences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_request_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("schedule_revision_id", sa.Uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("materialization_owner", sa.Uuid(), nullable=True),
        sa.Column("materialization_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('planned', 'materialized', 'skipped', 'cancelled')",
            name="ck_plan_occurrences_status",
        ),
        sa.CheckConstraint(
            "(materialization_owner IS NULL AND materialization_expires_at IS NULL) OR "
            "(materialization_owner IS NOT NULL "
            "AND materialization_expires_at IS NOT NULL)",
            name="ck_plan_occurrences_claim",
        ),
        sa.CheckConstraint("ordinal > 0", name="ck_plan_occurrences_ordinal"),
        sa.CheckConstraint("row_version > 0", name="ck_plan_occurrences_version"),
        sa.ForeignKeyConstraint(["schedule_id"], ["task_schedules.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["schedule_revision_id"], ["task_schedule_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_request_id"], ["task_requests.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_request_id", "ordinal", name="uq_plan_occurrences_request_ordinal"
        ),
    )
    op.create_index(
        "ix_plan_occurrences_due",
        "plan_occurrences",
        ["status", "scheduled_for", "materialization_expires_at", "ordinal", "id"],
        unique=False,
    )
    op.create_index(
        "ix_plan_occurrences_schedule", "plan_occurrences", ["schedule_id", "ordinal"], unique=False
    )
    op.create_index(
        "uq_plan_occurrences_request_time",
        "plan_occurrences",
        ["schedule_id", "scheduled_for"],
        unique=True,
        postgresql_where=sa.text(
            "scheduled_for IS NOT NULL AND status IN ('planned', 'materialized')"
        ),
    )
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_request_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("target_device_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(status = 'ended' AND result IN ('success', 'failure')) OR "
            "(status <> 'ended' AND result IS NULL)",
            name="ck_executions_result",
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'queued', 'running', 'ended', 'cancelled', 'timed_out')",
            name="ck_executions_status",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_executions_version"),
        sa.CheckConstraint(
            "timeout_at IS NULL OR timeout_at > created_at", name="ck_executions_timeout"
        ),
        sa.ForeignKeyConstraint(["occurrence_id"], ["plan_occurrences.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_request_id"], ["task_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("occurrence_id", name="uq_executions_occurrence"),
    )
    op.create_index(
        "ix_executions_target_status", "executions", ["target_device_id", "status"], unique=False
    )
    op.create_index(
        "ix_executions_task", "executions", ["task_request_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_executions_terminal_status", "executions", ["terminal_id", "status"], unique=False
    )
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(status = 'ended' AND result IN ('success', 'failure')) OR "
            "(status <> 'ended' AND result IS NULL)",
            name="ck_execution_attempts_result",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ended', 'cancelled', 'timed_out')",
            name="ck_execution_attempts_status",
        ),
        sa.CheckConstraint("attempt_no > 0", name="ck_execution_attempts_number"),
        sa.CheckConstraint("row_version > 0", name="ck_execution_attempts_version"),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lease_id"], ["execution_leases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", "attempt_no", name="uq_execution_attempts_number"),
    )
    op.create_index(
        "ix_execution_attempts_fifo",
        "execution_attempts",
        ["status", "available_at", "enqueued_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_execution_attempts_active",
        "execution_attempts",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "execution_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("source_module", sa.String(length=80), nullable=False),
        sa.Column("logical_content_id", sa.String(length=255), nullable=False),
        sa.Column("revision_id", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "capability_requirements", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("target_device_id", sa.Uuid(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("record_video", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_execution_snapshots_hash"),
        sa.CheckConstraint(
            "max_retries >= 0 AND max_retries <= 100", name="ck_execution_snapshots_retries"
        ),
        sa.CheckConstraint("schema_version > 0", name="ck_execution_snapshots_schema"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_execution_snapshots_timeout"),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", name="uq_execution_snapshots_execution"),
    )
    op.create_index(
        "ix_execution_snapshots_source",
        "execution_snapshots",
        ["source_module", "logical_content_id"],
        unique=False,
    )
    op.create_table(
        "execution_snapshot_blobs",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("resource_key", sa.String(length=255), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["blob_id"], ["blob_objects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["execution_snapshots.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("snapshot_id", "resource_key"),
        sa.UniqueConstraint("snapshot_id", "blob_id", "role", name="uq_snapshot_blobs_role"),
    )
    op.create_index(
        "ix_execution_snapshot_blobs_blob", "execution_snapshot_blobs", ["blob_id"], unique=False
    )
    op.create_table(
        "task_retry_origins",
        sa.Column("task_request_id", sa.Uuid(), nullable=False),
        sa.Column("source_execution_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_request_id"], ["task_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"], ["execution_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("task_request_id"),
    )
    op.create_index(
        "ix_task_retry_origins_source", "task_retry_origins", ["source_execution_id"], unique=False
    )
    op.execute(
        """
        CREATE FUNCTION al1s_reject_immutable_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_execution_snapshots_immutable
        BEFORE UPDATE ON execution_snapshots
        FOR EACH ROW EXECUTE FUNCTION al1s_reject_immutable_update()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_schedule_revisions_immutable
        BEFORE UPDATE ON task_schedule_revisions
        FOR EACH ROW EXECUTE FUNCTION al1s_reject_immutable_update()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_schedule_revision_times_immutable
        BEFORE UPDATE ON task_schedule_revision_times
        FOR EACH ROW EXECUTE FUNCTION al1s_reject_immutable_update()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_state_transitions_immutable
        BEFORE UPDATE ON state_transitions
        FOR EACH ROW EXECUTE FUNCTION al1s_reject_immutable_update()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_state_transitions_immutable ON state_transitions")
    op.execute(
        "DROP TRIGGER trg_task_schedule_revision_times_immutable "
        "ON task_schedule_revision_times"
    )
    op.execute(
        "DROP TRIGGER trg_task_schedule_revisions_immutable ON task_schedule_revisions"
    )
    op.execute("DROP TRIGGER trg_execution_snapshots_immutable ON execution_snapshots")
    op.execute("DROP FUNCTION al1s_reject_immutable_update()")
    op.drop_index("ix_task_retry_origins_source", table_name="task_retry_origins")
    op.drop_table("task_retry_origins")
    op.drop_index("ix_execution_snapshot_blobs_blob", table_name="execution_snapshot_blobs")
    op.drop_table("execution_snapshot_blobs")
    op.drop_index("ix_execution_snapshots_source", table_name="execution_snapshots")
    op.drop_table("execution_snapshots")
    op.drop_index(
        "uq_execution_attempts_active",
        table_name="execution_attempts",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_execution_attempts_fifo", table_name="execution_attempts")
    op.drop_table("execution_attempts")
    op.drop_index("ix_executions_terminal_status", table_name="executions")
    op.drop_index("ix_executions_task", table_name="executions")
    op.drop_index("ix_executions_target_status", table_name="executions")
    op.drop_table("executions")
    op.drop_index(
        "uq_plan_occurrences_request_time",
        table_name="plan_occurrences",
        postgresql_where=sa.text(
            "scheduled_for IS NOT NULL AND status IN ('planned', 'materialized')"
        ),
    )
    op.drop_index("ix_plan_occurrences_schedule", table_name="plan_occurrences")
    op.drop_index("ix_plan_occurrences_due", table_name="plan_occurrences")
    op.drop_table("plan_occurrences")
    op.drop_table("task_schedule_revision_times")
    op.drop_index(
        "ix_task_schedule_revisions_schedule", table_name="task_schedule_revisions"
    )
    op.drop_table("task_schedule_revisions")
    op.drop_index("ix_task_schedules_active", table_name="task_schedules")
    op.drop_table("task_schedules")
    op.drop_index("ix_task_requests_target", table_name="task_requests")
    op.drop_index("ix_task_requests_history", table_name="task_requests")
    op.drop_table("task_requests")
    op.drop_index("ix_state_transitions_correlation", table_name="state_transitions")
    op.drop_index("ix_state_transitions_aggregate", table_name="state_transitions")
    op.drop_table("state_transitions")
