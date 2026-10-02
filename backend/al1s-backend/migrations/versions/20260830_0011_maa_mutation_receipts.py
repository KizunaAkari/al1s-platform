"""Create atomic Maa management mutation receipts.

Revision ID: 20260830_0011
Revises: 20260830_0010
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_0011"
down_revision: str | None = "20260830_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maa_mutation_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column(
            "response_body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(operation) BETWEEN 1 AND 255",
            name="ck_maa_mutation_operation",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_maa_mutation_idempotency",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maa_mutation_request_hash",
        ),
        sa.CheckConstraint(
            "char_length(aggregate_type) BETWEEN 1 AND 64",
            name="ck_maa_mutation_aggregate_type",
        ),
        sa.CheckConstraint(
            "response_status BETWEEN 200 AND 299",
            name="ck_maa_mutation_response_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation", "idempotency_key", name="uq_maa_mutation_idempotency"
        ),
    )
    op.create_index(
        "ix_maa_mutation_expiry", "maa_mutation_receipts", ["expires_at", "id"]
    )
    op.execute(
        """
        CREATE TRIGGER trg_maa_mutation_receipts_immutable
        BEFORE UPDATE ON maa_mutation_receipts
        FOR EACH ROW EXECUTE FUNCTION al1s_reject_immutable_update()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_maa_mutation_receipts_immutable ON maa_mutation_receipts"
    )
    op.drop_index("ix_maa_mutation_expiry", table_name="maa_mutation_receipts")
    op.drop_table("maa_mutation_receipts")
