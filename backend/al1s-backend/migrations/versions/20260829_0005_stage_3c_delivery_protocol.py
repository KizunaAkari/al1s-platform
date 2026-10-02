"""Create Stage 3C delivery protocol facts.

Revision ID: 20260829_0005
Revises: 20260829_0004
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0005"
down_revision: str | None = "20260829_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("cancel_reason", sa.String(length=32)))
    op.add_column("executions", sa.Column("cancel_deadline_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_executions_cancel_reason",
        "executions",
        "cancel_reason IS NULL OR cancel_reason IN ('operator', 'timeout')",
    )
    op.create_check_constraint(
        "ck_executions_cancel_window",
        "executions",
        "(cancel_requested_at IS NULL AND cancel_reason IS NULL AND cancel_deadline_at IS NULL) OR "
        "(cancel_requested_at IS NOT NULL AND cancel_reason IS NOT NULL "
        "AND (cancel_deadline_at IS NULL OR cancel_deadline_at > cancel_requested_at))",
    )
    op.add_column("execution_attempts", sa.Column("failure_phase", sa.String(length=32)))
    op.create_check_constraint(
        "ck_execution_attempts_failure_phase",
        "execution_attempts",
        "failure_phase IS NULL OR failure_phase IN ('delivery', 'runtime', 'cleanup', 'platform')",
    )
    op.drop_constraint("ck_execution_leases_owner", "execution_leases", type_="check")
    op.create_check_constraint(
        "ck_execution_leases_owner",
        "execution_leases",
        "owner_kind IN "
        "('execution', 'execution_attempt', 'remote_session', 'quick_test', 'system')",
    )

    op.create_table(
        "task_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("target_device_id", sa.Uuid()),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("package_schema_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False),
        sa.Column("package_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("delivery_attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_delivery_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_rejection_disposition", sa.String(length=32)),
        sa.Column("last_rejection_code", sa.String(length=64)),
        sa.Column("last_rejection_diagnostic", sa.String(length=512)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("protocol_version > 0", name="ck_task_packages_protocol"),
        sa.CheckConstraint("package_schema_version > 0", name="ck_task_packages_schema"),
        sa.CheckConstraint(
            "snapshot_schema_version > 0", name="ck_task_packages_snapshot_schema"
        ),
        sa.CheckConstraint("delivery_attempt_count > 0", name="ck_task_packages_delivery_count"),
        sa.CheckConstraint("row_version > 0", name="ck_task_packages_version"),
        sa.CheckConstraint(
            "status IN ('available', 'accepted', 'delivery_failed', 'cancelled')",
            name="ck_task_packages_status",
        ),
        sa.CheckConstraint(
            "last_rejection_disposition IS NULL OR "
            "last_rejection_disposition IN "
            "('retryable_wait', 'blocked_wait', 'permanent_failure')",
            name="ck_task_packages_rejection_disposition",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["execution_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_task_packages_attempt"),
    )
    op.create_index(
        "ix_task_packages_terminal_pending",
        "task_packages",
        ["terminal_id", "status", "next_delivery_at", "created_at", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_task_package_content_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'task package content is immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_task_package_content_immutable
        BEFORE UPDATE OF attempt_id, execution_id, snapshot_id, terminal_id, target_device_id,
            protocol_version, package_schema_version, snapshot_schema_version, package_hash,
            manifest,
            created_at
        ON task_packages FOR EACH ROW EXECUTE FUNCTION prevent_task_package_content_update();
        """
    )

    op.create_table(
        "commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("command_kind", sa.String(length=32), nullable=False),
        sa.Column("package_id", sa.Uuid()),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "command_kind IN ('task_package_available', 'cancel_requested')",
            name="ck_commands_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'acknowledged', 'superseded')",
            name="ck_commands_status",
        ),
        sa.CheckConstraint("delivery_no > 0", name="ck_commands_delivery_no"),
        sa.CheckConstraint("row_version > 0", name="ck_commands_version"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["package_id"], ["task_packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id", "command_kind", "delivery_no", name="uq_commands_attempt_delivery"
        ),
    )
    op.create_index(
        "ix_commands_terminal_pending",
        "commands",
        ["terminal_id", "status", "available_at", "created_at", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_command_envelope_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'command envelope is immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_command_envelope_immutable
        BEFORE UPDATE OF terminal_id, command_kind, package_id, attempt_id, delivery_no, payload,
            available_at, created_at
        ON commands FOR EACH ROW EXECUTE FUNCTION prevent_command_envelope_update();
        """
    )

    op.create_table(
        "terminal_reports",
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("report_kind", sa.String(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("command_id", sa.Uuid()),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("package_id", sa.Uuid()),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("diagnostic", sa.String(length=512)),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "report_kind IN "
            "('package_receipt', 'attempt_start', 'attempt_result')",
            name="ck_terminal_reports_kind",
        ),
        sa.CheckConstraint(
            "disposition IN ('accepted', 'duplicate', 'stale', 'rejected')",
            name="ck_terminal_reports_disposition",
        ),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["commands.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["package_id"], ["task_packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lease_id"], ["execution_leases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("terminal_id", "report_id"),
    )
    op.create_index(
        "ix_terminal_reports_attempt", "terminal_reports", ["attempt_id", "received_at"]
    )
    op.create_index("ix_terminal_reports_received", "terminal_reports", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_terminal_reports_received", table_name="terminal_reports")
    op.drop_index("ix_terminal_reports_attempt", table_name="terminal_reports")
    op.drop_table("terminal_reports")
    op.execute("DROP TRIGGER trg_command_envelope_immutable ON commands")
    op.execute("DROP FUNCTION prevent_command_envelope_update")
    op.drop_index("ix_commands_terminal_pending", table_name="commands")
    op.drop_table("commands")
    op.execute("DROP TRIGGER trg_task_package_content_immutable ON task_packages")
    op.execute("DROP FUNCTION prevent_task_package_content_update")
    op.drop_index("ix_task_packages_terminal_pending", table_name="task_packages")
    op.drop_table("task_packages")
    op.drop_constraint("ck_execution_leases_owner", "execution_leases", type_="check")
    op.create_check_constraint(
        "ck_execution_leases_owner",
        "execution_leases",
        "owner_kind IN ('execution', 'remote_session', 'quick_test', 'system')",
    )
    op.drop_constraint(
        "ck_execution_attempts_failure_phase", "execution_attempts", type_="check"
    )
    op.drop_column("execution_attempts", "failure_phase")
    op.drop_constraint("ck_executions_cancel_window", "executions", type_="check")
    op.drop_constraint("ck_executions_cancel_reason", "executions", type_="check")
    op.drop_column("executions", "cancel_deadline_at")
    op.drop_column("executions", "cancel_reason")
