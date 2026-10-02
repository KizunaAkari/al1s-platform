"""Persist one controlled manual storage cleanup request at a time."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0045"
down_revision = "20260925_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_gc_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(100)),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_storage_gc_requests_status",
        ),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_storage_gc_requests_attempts"),
        sa.CheckConstraint(
            "(status <> 'processing') OR locked_until IS NOT NULL",
            name="ck_storage_gc_requests_lease",
        ),
    )
    op.create_index(
        "uq_storage_gc_requests_active", "storage_gc_requests", [sa.text("(1)")], unique=True,
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
    op.create_index(
        "ix_storage_gc_requests_requested", "storage_gc_requests", ["requested_at", "id"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM storage_gc_requests)")):
        raise RuntimeError("0045 downgrade would erase storage cleanup history")
    op.drop_index("ix_storage_gc_requests_requested", table_name="storage_gc_requests")
    op.drop_index("uq_storage_gc_requests_active", table_name="storage_gc_requests")
    op.drop_table("storage_gc_requests")
