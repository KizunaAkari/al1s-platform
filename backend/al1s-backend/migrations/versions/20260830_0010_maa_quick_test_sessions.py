"""Create terminal-bound Maa quick-test sessions.

Revision ID: 20260830_0010
Revises: 20260830_0009
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0010"
down_revision: str | None = "20260830_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maa_quick_test_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("script_id", sa.Uuid(), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("target_device_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("qualification_receipt_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maa_quick_test_manifest_hash",
        ),
        sa.CheckConstraint(
            "definition_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maa_quick_test_definition_hash",
        ),
        sa.CheckConstraint(
            "status IN ('issued', 'completed', 'expired')",
            name="ck_maa_quick_test_status",
        ),
        sa.CheckConstraint(
            "char_length(request_idempotency_key) BETWEEN 1 AND 128",
            name="ck_maa_quick_test_idempotency",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_maa_quick_test_version"),
        sa.CheckConstraint(
            "(status = 'issued' AND completed_at IS NULL "
            "AND qualification_receipt_id IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND qualification_receipt_id IS NOT NULL) OR "
            "(status = 'expired' AND completed_at IS NOT NULL "
            "AND qualification_receipt_id IS NULL)",
            name="ck_maa_quick_test_completion",
        ),
        sa.ForeignKeyConstraint(["script_id"], ["maa_scripts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["script_version_id"], ["maa_script_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["qualification_receipt_id"],
            ["maa_script_qualification_receipts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "script_version_id",
            "request_idempotency_key",
            name="uq_maa_quick_test_idempotency",
        ),
        sa.UniqueConstraint(
            "qualification_receipt_id", name="uq_maa_quick_test_receipt"
        ),
    )
    op.create_index(
        "ix_maa_quick_test_terminal_pending",
        "maa_quick_test_sessions",
        ["terminal_id", "status", "expires_at", "id"],
    )
    op.create_index(
        "ix_maa_quick_test_expiry",
        "maa_quick_test_sessions",
        ["status", "expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_maa_quick_test_expiry", table_name="maa_quick_test_sessions")
    op.drop_index(
        "ix_maa_quick_test_terminal_pending", table_name="maa_quick_test_sessions"
    )
    op.drop_table("maa_quick_test_sessions")
