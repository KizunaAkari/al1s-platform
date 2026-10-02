"""Create immutable Maa combination strategy versions.

Revision ID: 20260830_0008
Revises: 20260830_0007
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_0008"
down_revision: str | None = "20260830_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maa_strategies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 255", name="ck_maa_strategies_name"
        ),
        sa.CheckConstraint(
            "char_length(normalized_name) BETWEEN 1 AND 255",
            name="ck_maa_strategies_normalized_name",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'retired')", name="ck_maa_strategies_status"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_maa_strategies_version"),
        sa.ForeignKeyConstraint(
            ["application_id"], ["maa_applications.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_maa_strategies_active_name",
        "maa_strategies",
        ["application_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_maa_strategies_list",
        "maa_strategies",
        ["application_id", "status", "normalized_name", "id"],
    )

    op.create_table(
        "maa_strategy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_maa_strategy_versions_revision"),
        sa.CheckConstraint(
            "schema_version > 0", name="ck_maa_strategy_versions_schema"
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maa_strategy_versions_hash",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"], ["maa_strategies.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "strategy_id", "revision", name="uq_maa_strategy_versions_revision"
        ),
        sa.UniqueConstraint(
            "strategy_id", "manifest_hash", name="uq_maa_strategy_versions_hash"
        ),
    )
    op.create_index(
        "ix_maa_strategy_versions_list",
        "maa_strategy_versions",
        ["strategy_id", "revision", "id"],
    )
    op.create_foreign_key(
        "fk_maa_strategies_current_version",
        "maa_strategies",
        "maa_strategy_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "maa_strategy_modules",
        sa.Column("strategy_version_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("module_role", sa.String(length=16), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("wait_after_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_maa_strategy_modules_position"),
        sa.CheckConstraint(
            "module_role IN ('start', 'process', 'end')",
            name="ck_maa_strategy_modules_role",
        ),
        sa.CheckConstraint(
            "wait_after_ms >= 0 AND wait_after_ms <= 14400000",
            name="ck_maa_strategy_modules_wait",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["maa_strategy_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["script_version_id"], ["maa_script_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("strategy_version_id", "position"),
    )
    op.create_index(
        "uq_maa_strategy_modules_start",
        "maa_strategy_modules",
        ["strategy_version_id"],
        unique=True,
        postgresql_where=sa.text("module_role = 'start'"),
    )
    op.create_index(
        "uq_maa_strategy_modules_end",
        "maa_strategy_modules",
        ["strategy_version_id"],
        unique=True,
        postgresql_where=sa.text("module_role = 'end'"),
    )
    op.create_index(
        "ix_maa_strategy_modules_script",
        "maa_strategy_modules",
        ["script_version_id", "strategy_version_id"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_maa_strategy_version_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Maa strategy versions are immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_maa_strategy_versions_immutable
        BEFORE UPDATE OR DELETE ON maa_strategy_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_maa_strategy_version_update();

        CREATE FUNCTION prevent_maa_strategy_module_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Maa strategy modules are immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_maa_strategy_modules_immutable
        BEFORE UPDATE OR DELETE ON maa_strategy_modules
        FOR EACH ROW EXECUTE FUNCTION prevent_maa_strategy_module_update();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_maa_strategy_modules_immutable ON maa_strategy_modules")
    op.execute("DROP FUNCTION prevent_maa_strategy_module_update")
    op.execute("DROP TRIGGER trg_maa_strategy_versions_immutable ON maa_strategy_versions")
    op.execute("DROP FUNCTION prevent_maa_strategy_version_update")
    op.drop_index("ix_maa_strategy_modules_script", table_name="maa_strategy_modules")
    op.drop_index("uq_maa_strategy_modules_end", table_name="maa_strategy_modules")
    op.drop_index("uq_maa_strategy_modules_start", table_name="maa_strategy_modules")
    op.drop_table("maa_strategy_modules")
    op.drop_constraint(
        "fk_maa_strategies_current_version", "maa_strategies", type_="foreignkey"
    )
    op.drop_index("ix_maa_strategy_versions_list", table_name="maa_strategy_versions")
    op.drop_table("maa_strategy_versions")
    op.drop_index("ix_maa_strategies_list", table_name="maa_strategies")
    op.drop_index("uq_maa_strategies_active_name", table_name="maa_strategies")
    op.drop_table("maa_strategies")
