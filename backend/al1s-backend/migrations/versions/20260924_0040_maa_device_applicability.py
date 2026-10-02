"""Allow phone-specific script categories for the same Android package."""

import sqlalchemy as sa
from alembic import op

revision = "20260924_0040"
down_revision = "20260921_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_maa_app_active_package", table_name="maa_applications")
    op.create_table(
        "maa_application_devices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("maa_applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.Uuid(),
            sa.ForeignKey("target_devices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("package_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_maa_app_device_package",
        "maa_application_devices",
        ["device_id", "package_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_maa_app_device_pair",
        "maa_application_devices",
        ["application_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_maa_app_devices_app", "maa_application_devices", ["application_id", "deleted_at"]
    )


def downgrade() -> None:
    # A global package unique index is unsafe after multiple categories exist.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM maa_applications WHERE deleted_at IS NULL "
        "GROUP BY package_name HAVING count(*) > 1) THEN "
        "RAISE EXCEPTION 'multiple categories share a package'; END IF; END $$"
    )
    op.drop_table("maa_application_devices")
    op.create_index(
        "uq_maa_app_active_package",
        "maa_applications",
        ["package_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
