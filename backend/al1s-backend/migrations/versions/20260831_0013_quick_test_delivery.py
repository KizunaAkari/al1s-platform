"""Persist immutable quick-test definitions and terminal claims.

Revision ID: 20260831_0013
Revises: 20260830_0012
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260831_0013"
down_revision: str | None = "20260830_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "maa_quick_test_sessions",
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "maa_quick_test_sessions",
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
    )
    # Definitions issued before this migration cannot be reconstructed safely.
    op.execute(
        "UPDATE maa_quick_test_sessions "
        "SET status = 'expired', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
        "definition = '{}'::jsonb "
        "WHERE definition IS NULL"
    )
    op.alter_column("maa_quick_test_sessions", "definition", nullable=False)
    op.drop_constraint("ck_maa_quick_test_status", "maa_quick_test_sessions", type_="check")
    op.drop_constraint("ck_maa_quick_test_completion", "maa_quick_test_sessions", type_="check")
    op.create_check_constraint(
        "ck_maa_quick_test_status",
        "maa_quick_test_sessions",
        "status IN ('issued', 'claimed', 'completed', 'expired')",
    )
    op.create_check_constraint(
        "ck_maa_quick_test_completion",
        "maa_quick_test_sessions",
        "(status = 'issued' AND claimed_at IS NULL AND completed_at IS NULL "
        "AND qualification_receipt_id IS NULL) OR "
        "(status = 'claimed' AND claimed_at IS NOT NULL AND completed_at IS NULL "
        "AND qualification_receipt_id IS NULL) OR "
        "(status = 'completed' AND claimed_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NOT NULL) OR "
        "(status = 'expired' AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NULL)",
    )
    op.create_table(
        "maa_quick_test_blobs",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("resource_key", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "char_length(resource_key) BETWEEN 1 AND 255",
            name="ck_maa_quick_test_blobs_resource_key",
        ),
        sa.CheckConstraint(
            "char_length(role) BETWEEN 1 AND 80",
            name="ck_maa_quick_test_blobs_role",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_maa_quick_test_blobs_ordinal"),
        sa.ForeignKeyConstraint(["session_id"], ["maa_quick_test_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["blob_id"], ["blob_objects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("session_id", "blob_id"),
        sa.UniqueConstraint(
            "session_id", "resource_key", name="uq_maa_quick_test_blobs_resource_key"
        ),
        sa.UniqueConstraint("session_id", "ordinal", name="uq_maa_quick_test_blobs_ordinal"),
    )
    op.create_index(
        "ix_maa_quick_test_blobs_blob",
        "maa_quick_test_blobs",
        ["blob_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_maa_quick_test_blobs_blob", table_name="maa_quick_test_blobs")
    op.drop_table("maa_quick_test_blobs")
    op.drop_constraint("ck_maa_quick_test_completion", "maa_quick_test_sessions", type_="check")
    op.drop_constraint("ck_maa_quick_test_status", "maa_quick_test_sessions", type_="check")
    op.execute(
        "UPDATE maa_quick_test_sessions "
        "SET status = 'expired', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
        "qualification_receipt_id = NULL "
        "WHERE status = 'claimed'"
    )
    op.create_check_constraint(
        "ck_maa_quick_test_status",
        "maa_quick_test_sessions",
        "status IN ('issued', 'completed', 'expired')",
    )
    op.create_check_constraint(
        "ck_maa_quick_test_completion",
        "maa_quick_test_sessions",
        "(status = 'issued' AND completed_at IS NULL "
        "AND qualification_receipt_id IS NULL) OR "
        "(status = 'completed' AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NOT NULL) OR "
        "(status = 'expired' AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NULL)",
    )
    op.drop_column("maa_quick_test_sessions", "claimed_at")
    op.drop_column("maa_quick_test_sessions", "definition")
