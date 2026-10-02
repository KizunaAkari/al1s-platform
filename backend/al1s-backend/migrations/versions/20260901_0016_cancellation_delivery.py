"""Persist cancellation delivery acknowledgements and delayed delivery facts.

Revision ID: 20260901_0016
Revises: 20260831_0015
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0016"
down_revision: str | None = "20260831_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "cancel_delivery_delayed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "executions",
        sa.Column("cancel_delivery_note", sa.String(length=100)),
    )
    op.create_check_constraint(
        "ck_executions_cancel_delivery",
        "executions",
        "(cancel_delivery_delayed = false AND cancel_delivery_note IS NULL) OR "
        "(cancel_delivery_delayed = true AND cancel_delivery_note IS NOT NULL)",
    )
    op.create_table(
        "command_acknowledgements",
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN "
            "('running_cancel_accepted', 'already_completed', 'cancelled_before_start')",
            name="ck_command_acknowledgements_outcome",
        ),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["commands.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["execution_attempts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("terminal_id", "report_id"),
        sa.UniqueConstraint("command_id", name="uq_command_acknowledgements_command"),
    )
    op.create_index(
        "ix_command_acknowledgements_attempt",
        "command_acknowledgements",
        ["attempt_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_command_acknowledgements_attempt",
        table_name="command_acknowledgements",
    )
    op.drop_table("command_acknowledgements")
    op.drop_constraint("ck_executions_cancel_delivery", "executions", type_="check")
    op.drop_column("executions", "cancel_delivery_note")
    op.drop_column("executions", "cancel_delivery_delayed")
