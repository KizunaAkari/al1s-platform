"""Create Stage 3A terminal-resource tables.

Revision ID: 20260829_0003
Revises: 20260828_0002
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0003"
down_revision: str | None = "20260828_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "terminals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("terminal_type", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("service_status", sa.String(length=32), nullable=False),
        sa.Column("acceptance_status", sa.String(length=32), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=False),
        sa.Column("current_capability_profile_id", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "acceptance_status IN ('accepting', 'draining', 'disabled')",
            name="ck_terminals_acceptance",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_terminals_version"),
        sa.CheckConstraint("service_status IN ('online', 'offline')", name="ck_terminals_service"),
        sa.CheckConstraint("terminal_type IN ('linux', 'android')", name="ck_terminals_type"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_terminals_status",
        "terminals",
        ["deleted_at", "service_status", "acceptance_status"],
    )
    op.create_index(
        "uq_terminals_active_installation",
        "terminals",
        ["installation_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "terminal_registration_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("secret_digest", sa.String(length=255), nullable=False),
        sa.Column("allowed_terminal_type", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_terminal_id", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "allowed_terminal_type IS NULL OR allowed_terminal_type IN ('linux', 'android')",
            name="ck_terminal_registration_grants_type",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_terminal_registration_grants_version"),
        sa.ForeignKeyConstraint(
            ["consumed_by_terminal_id"],
            ["terminals.id"],
            name="fk_terminal_registration_grants_consumed_terminal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_terminal_registration_grants_expires",
        "terminal_registration_grants",
        ["expires_at"],
    )

    op.create_table(
        "terminal_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("secret_digest", sa.String(length=255), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("row_version > 0", name="ck_terminal_credentials_version"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_terminal_credentials_terminal",
        "terminal_credentials",
        ["terminal_id", "created_at"],
    )
    op.create_index(
        "uq_terminal_credentials_active",
        "terminal_credentials",
        ["terminal_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "terminal_capability_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=False),
        sa.Column("os_name", sa.String(length=64), nullable=False),
        sa.Column("os_version", sa.String(length=128), nullable=False),
        sa.Column("architecture", sa.String(length=64), nullable=False),
        sa.Column("cpu_cores", sa.Integer(), nullable=False),
        sa.Column("memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_available_bytes", sa.BigInteger(), nullable=False),
        sa.Column("accelerator_type", sa.String(length=64), nullable=True),
        sa.Column("low_resource", sa.Boolean(), nullable=False),
        sa.Column("provider_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("cpu_cores > 0", name="ck_terminal_capabilities_cpu"),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_terminal_capabilities_hash"
        ),
        sa.CheckConstraint("memory_bytes >= 0", name="ck_terminal_capabilities_memory"),
        sa.CheckConstraint("protocol_version > 0", name="ck_terminal_capabilities_protocol"),
        sa.CheckConstraint("revision > 0", name="ck_terminal_capabilities_revision"),
        sa.CheckConstraint("schema_version > 0", name="ck_terminal_capabilities_schema"),
        sa.CheckConstraint("storage_available_bytes >= 0", name="ck_terminal_capabilities_storage"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_terminal_capabilities_match",
        "terminal_capability_profiles",
        ["architecture", "accelerator_type"],
    )
    op.create_index(
        "ix_terminal_capabilities_terminal_created",
        "terminal_capability_profiles",
        ["terminal_id", "created_at"],
    )
    op.create_index(
        "uq_terminal_capabilities_revision",
        "terminal_capability_profiles",
        ["terminal_id", "revision"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_terminals_current_capability_profile",
        "terminals",
        "terminal_capability_profiles",
        ["current_capability_profile_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "target_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("managing_terminal_id", sa.Uuid(), nullable=True),
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
            "(mode = 'unassigned' AND managing_terminal_id IS NULL) OR "
            "(mode <> 'unassigned' AND managing_terminal_id IS NOT NULL)",
            name="ck_target_devices_manager",
        ),
        sa.CheckConstraint(
            "mode IN ('unassigned', 'standalone', 'mounted')",
            name="ck_target_devices_mode",
        ),
        sa.CheckConstraint("platform = 'android'", name="ck_target_devices_platform"),
        sa.CheckConstraint("row_version > 0", name="ck_target_devices_version"),
        sa.ForeignKeyConstraint(["managing_terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_target_devices_manager",
        "target_devices",
        ["managing_terminal_id", "deleted_at"],
    )

    op.create_table(
        "target_device_identifiers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_device_id", sa.Uuid(), nullable=True),
        sa.Column("source_terminal_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("identifier_digest", sa.String(length=64), nullable=False),
        sa.Column("display_hint", sa.String(length=32), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "identifier_digest ~ '^[0-9a-f]{64}$'", name="ck_target_identifiers_digest"
        ),
        sa.CheckConstraint(
            "source_type IN ('apk_installation', 'adb_serial', 'android_id')",
            name="ck_target_identifiers_source",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_target_identifiers_version"),
        sa.ForeignKeyConstraint(["source_terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_target_identifiers_source_terminal",
        "target_device_identifiers",
        ["source_terminal_id", "created_at"],
    )
    op.create_index(
        "ix_target_identifiers_target",
        "target_device_identifiers",
        ["target_device_id", "deleted_at"],
    )
    op.create_index(
        "uq_target_identifiers_active_source",
        "target_device_identifiers",
        ["source_type", "identifier_digest"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "execution_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=True),
        sa.Column("target_device_id", sa.Uuid(), nullable=True),
        sa.Column("lease_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("expires_at > acquired_at", name="ck_execution_leases_expiry"),
        sa.CheckConstraint(
            "lease_kind IN ('execution', 'remote_control', 'quick_test', 'maintenance')",
            name="ck_execution_leases_kind",
        ),
        sa.CheckConstraint(
            "owner_kind IN ('execution', 'remote_session', 'quick_test', 'system')",
            name="ck_execution_leases_owner",
        ),
        sa.CheckConstraint(
            "terminal_id IS NOT NULL OR target_device_id IS NOT NULL",
            name="ck_execution_leases_resource",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_execution_leases_version"),
        sa.ForeignKeyConstraint(["target_device_id"], ["target_devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_execution_leases_expiry", "execution_leases", ["released_at", "expires_at"])
    op.create_index("ix_execution_leases_owner", "execution_leases", ["owner_kind", "owner_id"])
    op.create_index(
        "uq_execution_leases_active_target",
        "execution_leases",
        ["target_device_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL AND target_device_id IS NOT NULL"),
    )
    op.create_index(
        "uq_execution_leases_active_terminal",
        "execution_leases",
        ["terminal_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL AND terminal_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_execution_leases_active_terminal", table_name="execution_leases")
    op.drop_index("uq_execution_leases_active_target", table_name="execution_leases")
    op.drop_index("ix_execution_leases_owner", table_name="execution_leases")
    op.drop_index("ix_execution_leases_expiry", table_name="execution_leases")
    op.drop_table("execution_leases")
    op.drop_index("uq_target_identifiers_active_source", table_name="target_device_identifiers")
    op.drop_index("ix_target_identifiers_target", table_name="target_device_identifiers")
    op.drop_index("ix_target_identifiers_source_terminal", table_name="target_device_identifiers")
    op.drop_table("target_device_identifiers")
    op.drop_index("ix_target_devices_manager", table_name="target_devices")
    op.drop_table("target_devices")
    op.drop_constraint("fk_terminals_current_capability_profile", "terminals", type_="foreignkey")
    op.drop_index("uq_terminal_capabilities_revision", table_name="terminal_capability_profiles")
    op.drop_index(
        "ix_terminal_capabilities_terminal_created",
        table_name="terminal_capability_profiles",
    )
    op.drop_index("ix_terminal_capabilities_match", table_name="terminal_capability_profiles")
    op.drop_table("terminal_capability_profiles")
    op.drop_index("uq_terminal_credentials_active", table_name="terminal_credentials")
    op.drop_index("ix_terminal_credentials_terminal", table_name="terminal_credentials")
    op.drop_table("terminal_credentials")
    op.drop_index(
        "ix_terminal_registration_grants_expires",
        table_name="terminal_registration_grants",
    )
    op.drop_table("terminal_registration_grants")
    op.drop_index("uq_terminals_active_installation", table_name="terminals")
    op.drop_index("ix_terminals_status", table_name="terminals")
    op.drop_table("terminals")
