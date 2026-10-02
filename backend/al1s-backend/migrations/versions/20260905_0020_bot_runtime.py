"""Create Bot runtime identity, configuration, and health records.

Revision ID: 20260905_0020
Revises: 20260905_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0020"
down_revision: str | None = "20260905_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("desired_config_version_id", sa.Uuid(), nullable=True),
        sa.Column("applied_config_version_id", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('onebot_gateway', 'discord_bridge')", name="ck_bot_services_kind"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_bot_services_row_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bot_services_created", "bot_services", ["created_at", "id"])
    op.create_index(
        "ix_bot_services_kind_created", "bot_services", ["kind", "created_at", "id"]
    )
    op.create_index(
        "uq_bot_services_active_name",
        "bot_services",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "bot_worker_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("credential_digest", sa.Text(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "credential_version > 0", name="ck_bot_worker_identity_version"
        ),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_id", name="uq_bot_worker_identity_service"),
    )
    op.create_index(
        "ix_bot_worker_identity_enabled",
        "bot_worker_identities",
        ["enabled", "last_seen_at"],
    )
    op.create_table(
        "bot_registration_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("secret_digest", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_identity_id", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "row_version > 0", name="ck_bot_registration_grants_row_version"
        ),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["consumed_by_identity_id"],
            ["bot_worker_identities.id"],
            name="fk_bot_registration_grants_consumed_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bot_registration_grants_service_created",
        "bot_registration_grants",
        ["service_id", "expires_at"],
    )
    op.create_table(
        "bot_config_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("secret_id", sa.Uuid(), nullable=True),
        sa.Column("onebot_service_id", sa.Uuid(), nullable=True),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("version_no > 0", name="ck_bot_config_versions_version"),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["secret_id"], ["encrypted_secrets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["onebot_service_id"], ["bot_services.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_id", "version_no", name="uq_bot_config_service_version"),
    )
    op.create_index(
        "ix_bot_config_versions_service_created",
        "bot_config_versions",
        ["service_id", "created_at", "id"],
    )
    op.create_foreign_key(
        "fk_bot_services_desired_config",
        "bot_services",
        "bot_config_versions",
        ["desired_config_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_bot_services_applied_config",
        "bot_services",
        "bot_config_versions",
        ["applied_config_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "bot_config_applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("config_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("worker_instance_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("receipt_event_id", sa.Uuid(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_bot_config_applications_status",
        ),
        sa.CheckConstraint(
            "status = 'pending' OR completed_at IS NOT NULL",
            name="ck_bot_config_applications_completed",
        ),
        sa.CheckConstraint(
            "row_version > 0", name="ck_bot_config_applications_row_version"
        ),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["config_version_id"], ["bot_config_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "config_version_id", name="uq_bot_config_application_version"
        ),
        sa.UniqueConstraint("receipt_event_id", name="uq_bot_config_application_receipt"),
    )
    op.create_index(
        "ix_bot_config_applications_service_status",
        "bot_config_applications",
        ["service_id", "status", "requested_at"],
    )
    op.create_index(
        "ix_bot_config_applications_service_requested",
        "bot_config_applications",
        ["service_id", "requested_at", "id"],
    )
    op.create_table(
        "bot_health_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_event_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("config_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('healthy', 'degraded', 'unhealthy')",
            name="ck_bot_health_reports_status",
        ),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["identity_id"], ["bot_worker_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["config_version_id"], ["bot_config_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_event_id", name="uq_bot_health_receipt"),
    )
    op.create_index(
        "ix_bot_health_service_reported",
        "bot_health_reports",
        ["service_id", "reported_at", "id"],
    )
    op.add_column("notification_channels", sa.Column("bot_service_id", sa.Uuid(), nullable=True))
    # Stage 7A development databases may already contain pre-binding QQ rows.
    # NOT VALID preserves those rows while enforcing the target contract for
    # every new or updated channel. Fresh production databases have no legacy rows.
    op.execute(
        "ALTER TABLE notification_channels "
        "ADD CONSTRAINT ck_notification_channels_bot_binding CHECK ("
        "(kind = 'smtp' AND bot_service_id IS NULL) OR "
        "(kind IN ('qq', 'discord') AND bot_service_id IS NOT NULL AND secret_id IS NULL)"
        ") NOT VALID"
    )
    op.create_foreign_key(
        "fk_notification_channels_bot_service",
        "notification_channels",
        "bot_services",
        ["bot_service_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_notification_channels_bot_service", "notification_channels", ["bot_service_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_notification_channels_bot_service", table_name="notification_channels")
    # Some development databases ran the first 0020 draft before this check was
    # added.  Keep the revision reversible for both shapes; production upgrades
    # still create and enforce the check above.
    op.execute(
        "ALTER TABLE notification_channels "
        "DROP CONSTRAINT IF EXISTS ck_notification_channels_bot_binding"
    )
    op.drop_constraint(
        "fk_notification_channels_bot_service", "notification_channels", type_="foreignkey"
    )
    op.drop_column("notification_channels", "bot_service_id")
    op.drop_table("bot_health_reports")
    op.drop_table("bot_config_applications")
    op.drop_constraint("fk_bot_services_applied_config", "bot_services", type_="foreignkey")
    op.drop_constraint("fk_bot_services_desired_config", "bot_services", type_="foreignkey")
    op.drop_table("bot_config_versions")
    op.drop_table("bot_registration_grants")
    op.drop_table("bot_worker_identities")
    op.drop_table("bot_services")
