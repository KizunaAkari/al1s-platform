"""Add Maa script candidates and immutable publication qualification receipts.

Revision ID: 20260830_0007
Revises: 20260830_0006
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_0007"
down_revision: str | None = "20260830_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "maa_scripts",
        sa.Column("candidate_version_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_maa_scripts_candidate_version",
        "maa_scripts",
        "maa_script_versions",
        ["candidate_version_id"],
        ["id"],
        ondelete="RESTRICT",
        use_alter=True,
    )
    op.create_index(
        "ix_maa_scripts_candidate",
        "maa_scripts",
        ["candidate_version_id"],
        postgresql_where=sa.text("candidate_version_id IS NOT NULL"),
    )

    # Stage 4A temporarily used current_version_id so imported versions were
    # queryable. They are candidates and must remain unavailable to formal tasks.
    op.execute(
        """
        UPDATE maa_scripts
        SET candidate_version_id = current_version_id,
            current_version_id = NULL
        WHERE status = 'validation_pending'
          AND candidate_version_id IS NULL
          AND current_version_id IS NOT NULL
        """
    )

    op.create_table(
        "maa_script_qualification_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("terminal_id", sa.Uuid()),
        sa.Column("target_device_id", sa.Uuid()),
        sa.Column("executor_version", sa.String(length=100), nullable=False),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column(
            "diagnostic",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maa_qualification_hash",
        ),
        sa.CheckConstraint(
            "kind IN ('static_check', 'quick_test')",
            name="ck_maa_qualification_kind",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed')",
            name="ck_maa_qualification_status",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_maa_qualification_idempotency",
        ),
        sa.CheckConstraint(
            "char_length(executor_version) BETWEEN 1 AND 100",
            name="ck_maa_qualification_executor",
        ),
        sa.CheckConstraint(
            "(status = 'passed' AND error_code IS NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL)",
            name="ck_maa_qualification_error",
        ),
        sa.CheckConstraint(
            "(kind = 'static_check' AND terminal_id IS NULL AND target_device_id IS NULL) OR "
            "(kind = 'quick_test' AND terminal_id IS NOT NULL "
            "AND target_device_id IS NOT NULL)",
            name="ck_maa_qualification_execution_context",
        ),
        sa.ForeignKeyConstraint(
            ["script_version_id"], ["maa_script_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "script_version_id",
            "kind",
            "idempotency_key",
            name="uq_maa_qualification_idempotency",
        ),
    )
    op.create_index(
        "ix_maa_qualification_gate",
        "maa_script_qualification_receipts",
        ["script_version_id", "kind", "status", sa.text("created_at DESC"), "id"],
    )
    op.create_index(
        "ix_maa_qualification_terminal",
        "maa_script_qualification_receipts",
        ["terminal_id", sa.text("created_at DESC"), "id"],
        postgresql_where=sa.text("terminal_id IS NOT NULL"),
    )

    op.execute(
        """
        CREATE FUNCTION prevent_maa_qualification_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Maa script qualification receipts are immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_maa_qualification_immutable
        BEFORE UPDATE OR DELETE ON maa_script_qualification_receipts
        FOR EACH ROW EXECUTE FUNCTION prevent_maa_qualification_update();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_maa_qualification_immutable "
        "ON maa_script_qualification_receipts"
    )
    op.execute("DROP FUNCTION prevent_maa_qualification_update")
    op.drop_index(
        "ix_maa_qualification_terminal",
        table_name="maa_script_qualification_receipts",
    )
    op.drop_index(
        "ix_maa_qualification_gate",
        table_name="maa_script_qualification_receipts",
    )
    op.drop_table("maa_script_qualification_receipts")

    # Restore the exact Stage 4A representation before removing the candidate
    # pointer so a downgrade remains lossless for imported pending scripts.
    op.execute(
        """
        UPDATE maa_scripts
        SET current_version_id = candidate_version_id
        WHERE status = 'validation_pending'
          AND current_version_id IS NULL
          AND candidate_version_id IS NOT NULL
        """
    )
    op.drop_index("ix_maa_scripts_candidate", table_name="maa_scripts")
    op.drop_constraint(
        "fk_maa_scripts_candidate_version", "maa_scripts", type_="foreignkey"
    )
    op.drop_column("maa_scripts", "candidate_version_id")
