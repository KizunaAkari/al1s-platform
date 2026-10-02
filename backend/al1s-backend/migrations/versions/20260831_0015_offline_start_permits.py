"""Create one-time offline start permits.

Revision ID: 20260831_0015
Revises: 20260831_0014
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0015"
down_revision: str | None = "20260831_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "offline_start_permits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("target_device_id", sa.Uuid()),
        sa.Column("package_hash", sa.String(length=64), nullable=False),
        sa.Column("permit_version", sa.Integer(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_start_report_id", sa.Uuid()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('issued', 'consumed', 'revoked', 'expired')",
            name="ck_offline_start_permits_status",
        ),
        sa.CheckConstraint("permit_version > 0", name="ck_offline_start_permits_protocol"),
        sa.CheckConstraint("row_version > 0", name="ck_offline_start_permits_version"),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_offline_start_permits_expiry"
        ),
        sa.CheckConstraint(
            "(status = 'issued' AND consumed_at IS NULL "
            "AND consumed_start_report_id IS NULL AND revoked_at IS NULL) OR "
            "(status = 'consumed' AND consumed_at IS NOT NULL "
            "AND consumed_start_report_id IS NOT NULL AND revoked_at IS NULL) OR "
            "(status IN ('revoked', 'expired') AND consumed_at IS NULL "
            "AND consumed_start_report_id IS NULL AND revoked_at IS NOT NULL)",
            name="ck_offline_start_permits_lifecycle",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["package_id"], ["task_packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_offline_start_permits_attempt"),
        sa.UniqueConstraint("token_digest", name="uq_offline_start_permits_token_digest"),
    )
    op.create_index(
        "ix_offline_start_permits_terminal_status",
        "offline_start_permits",
        ["terminal_id", "status", "issued_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_offline_start_permits_terminal_status", table_name="offline_start_permits"
    )
    op.drop_table("offline_start_permits")
