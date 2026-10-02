"""Create terminal artifact upload sessions and durable artifact records.

Revision ID: 20260831_0014
Revises: 20260831_0013
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0014"
down_revision: str | None = "20260831_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "terminal_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_kind", sa.String(length=32), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("blob_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "owner_kind IN ('formal_attempt', 'quick_test')",
            name="ck_terminal_artifacts_owner_kind",
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('screenshot', 'video', 'log', 'diagnostic')",
            name="ck_terminal_artifacts_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'expired')",
            name="ck_terminal_artifacts_status",
        ),
        sa.CheckConstraint(
            "expected_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_terminal_artifacts_sha256",
        ),
        sa.CheckConstraint("expected_size_bytes >= 0", name="ck_terminal_artifacts_size"),
        sa.CheckConstraint(
            "char_length(file_name) BETWEEN 1 AND 255",
            name="ck_terminal_artifacts_file_name",
        ),
        sa.CheckConstraint(
            "char_length(media_type) BETWEEN 1 AND 255",
            name="ck_terminal_artifacts_media_type",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_terminal_artifacts_idempotency",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_terminal_artifacts_version"),
        sa.CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND blob_id IS NULL) OR "
            "(status = 'ready' AND completed_at IS NOT NULL AND blob_id IS NOT NULL) OR "
            "(status = 'expired' AND completed_at IS NOT NULL AND blob_id IS NULL)",
            name="ck_terminal_artifacts_completion",
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["blob_objects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint(
            "terminal_id", "idempotency_key", name="uq_terminal_artifacts_idempotency"
        ),
    )
    op.create_index(
        "ix_terminal_artifacts_owner",
        "terminal_artifacts",
        ["owner_kind", "owner_id", "created_at", "id"],
    )
    op.create_index(
        "ix_terminal_artifacts_expiry",
        "terminal_artifacts",
        ["status", "expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_terminal_artifacts_expiry", table_name="terminal_artifacts")
    op.drop_index("ix_terminal_artifacts_owner", table_name="terminal_artifacts")
    op.drop_table("terminal_artifacts")
