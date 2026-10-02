"""Create Stage 4A Maa script assets and import facts.

Revision ID: 20260830_0006
Revises: 20260829_0005
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_0006"
down_revision: str | None = "20260829_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maa_applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("package_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "char_length(package_name) BETWEEN 1 AND 255", name="ck_maa_app_package"
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 255", name="ck_maa_app_name"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_maa_app_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_maa_app_active_package",
        "maa_applications",
        ["package_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_maa_app_list", "maa_applications", ["deleted_at", "display_name", "id"]
    )

    op.create_table(
        "maa_scripts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("script_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 255", name="ck_maa_scripts_name"),
        sa.CheckConstraint(
            "char_length(normalized_name) BETWEEN 1 AND 255",
            name="ck_maa_scripts_normalized_name",
        ),
        sa.CheckConstraint(
            "script_type IN ('standard', 'module_start', 'module_process', 'module_end')",
            name="ck_maa_scripts_type",
        ),
        sa.CheckConstraint(
            "status IN ('validation_pending', 'active', 'retired')",
            name="ck_maa_scripts_status",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_maa_scripts_version"),
        sa.ForeignKeyConstraint(
            ["application_id"], ["maa_applications.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_maa_scripts_active_name",
        "maa_scripts",
        ["application_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_maa_scripts_active_start",
        "maa_scripts",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND script_type = 'module_start'"),
    )
    op.create_index(
        "uq_maa_scripts_active_end",
        "maa_scripts",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND script_type = 'module_end'"),
    )
    op.create_index(
        "ix_maa_scripts_list",
        "maa_scripts",
        ["application_id", "status", "normalized_name", "id"],
    )

    op.create_table(
        "maa_script_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("script_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_maa_script_versions_revision"),
        sa.CheckConstraint("schema_version > 0", name="ck_maa_script_versions_schema"),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_maa_script_versions_hash"
        ),
        sa.ForeignKeyConstraint(["script_id"], ["maa_scripts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("script_id", "revision", name="uq_maa_script_versions_revision"),
        sa.UniqueConstraint("script_id", "manifest_hash", name="uq_maa_script_versions_hash"),
    )
    op.create_index(
        "ix_maa_script_versions_list",
        "maa_script_versions",
        ["script_id", "revision", "id"],
    )
    op.create_foreign_key(
        "fk_maa_scripts_current_version",
        "maa_scripts",
        "maa_script_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "maa_script_version_blobs",
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("json_pointer", sa.String(length=1024), nullable=False),
        sa.Column("resource_role", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_maa_script_blobs_ordinal"),
        sa.CheckConstraint(
            "char_length(json_pointer) BETWEEN 1 AND 1024",
            name="ck_maa_script_blobs_pointer",
        ),
        sa.CheckConstraint(
            "char_length(resource_role) BETWEEN 1 AND 64", name="ck_maa_script_blobs_role"
        ),
        sa.ForeignKeyConstraint(
            ["script_version_id"], ["maa_script_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["blob_objects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("script_version_id", "json_pointer"),
        sa.UniqueConstraint(
            "script_version_id", "ordinal", name="uq_maa_script_blobs_ordinal"
        ),
    )
    op.create_index(
        "ix_maa_script_blobs_blob",
        "maa_script_version_blobs",
        ["blob_id", "script_version_id"],
    )

    op.create_table(
        "maa_import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("logical_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_schema", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("script_count", sa.Integer(), nullable=False),
        sa.Column("application_count", sa.Integer(), nullable=False),
        sa.Column("resource_reference_count", sa.BigInteger(), nullable=False),
        sa.Column("unique_resource_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("diagnostic", sa.String(length=512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "logical_sha256 ~ '^[0-9a-f]{64}$'", name="ck_maa_import_logical_hash"
        ),
        sa.CheckConstraint(
            "archive_sha256 ~ '^[0-9a-f]{64}$'", name="ck_maa_import_archive_hash"
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')", name="ck_maa_import_status"
        ),
        sa.CheckConstraint(
            "script_count >= 0 AND application_count >= 0 "
            "AND resource_reference_count >= 0 AND unique_resource_count >= 0",
            name="ck_maa_import_counts",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_maa_import_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("logical_sha256", name="uq_maa_import_logical_hash"),
    )
    op.create_index(
        "ix_maa_import_list", "maa_import_batches", ["created_at", "id"]
    )

    op.create_table(
        "maa_import_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("archive_ordinal", sa.Integer(), nullable=False),
        sa.Column("script_name", sa.String(length=255), nullable=False),
        sa.Column("application_package", sa.String(length=255)),
        sa.Column("script_id", sa.Uuid()),
        sa.Column("script_version_id", sa.Uuid()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("migration_code", sa.String(length=100)),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("diagnostic", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("archive_ordinal > 0", name="ck_maa_import_items_ordinal"),
        sa.CheckConstraint(
            "status IN ('imported', 'rejected')", name="ck_maa_import_items_status"
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["maa_import_batches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["script_id"], ["maa_scripts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["script_version_id"], ["maa_script_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "archive_ordinal", name="uq_maa_import_items_ordinal"),
    )
    op.create_index(
        "ix_maa_import_items_page",
        "maa_import_items",
        ["batch_id", "archive_ordinal", "id"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_maa_script_version_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Maa script versions are immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_maa_script_versions_immutable
        BEFORE UPDATE OR DELETE ON maa_script_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_maa_script_version_update();

        CREATE FUNCTION prevent_maa_script_blob_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Maa script version blob references are immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_maa_script_blobs_immutable
        BEFORE UPDATE OR DELETE ON maa_script_version_blobs
        FOR EACH ROW EXECUTE FUNCTION prevent_maa_script_blob_update();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_maa_script_blobs_immutable ON maa_script_version_blobs")
    op.execute("DROP FUNCTION prevent_maa_script_blob_update")
    op.execute("DROP TRIGGER trg_maa_script_versions_immutable ON maa_script_versions")
    op.execute("DROP FUNCTION prevent_maa_script_version_update")
    op.drop_index("ix_maa_import_items_page", table_name="maa_import_items")
    op.drop_table("maa_import_items")
    op.drop_index("ix_maa_import_list", table_name="maa_import_batches")
    op.drop_table("maa_import_batches")
    op.drop_index("ix_maa_script_blobs_blob", table_name="maa_script_version_blobs")
    op.drop_table("maa_script_version_blobs")
    op.drop_constraint("fk_maa_scripts_current_version", "maa_scripts", type_="foreignkey")
    op.drop_index("ix_maa_script_versions_list", table_name="maa_script_versions")
    op.drop_table("maa_script_versions")
    op.drop_index("ix_maa_scripts_list", table_name="maa_scripts")
    op.drop_index("uq_maa_scripts_active_end", table_name="maa_scripts")
    op.drop_index("uq_maa_scripts_active_start", table_name="maa_scripts")
    op.drop_index("uq_maa_scripts_active_name", table_name="maa_scripts")
    op.drop_table("maa_scripts")
    op.drop_index("ix_maa_app_list", table_name="maa_applications")
    op.drop_index("uq_maa_app_active_package", table_name="maa_applications")
    op.drop_table("maa_applications")
