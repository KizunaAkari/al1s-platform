"""Android path observations and physical release authority, preserving old ownership."""

import sqlalchemy as sa
from alembic import op

revision = "20261006_0053"
down_revision = "20261004_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_paths",
        sa.Column(
            "device_id",
            sa.Uuid(),
            sa.ForeignKey("target_devices.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column(
            "terminal_id",
            sa.Uuid(),
            sa.ForeignKey("terminals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("instance_id", sa.Uuid()),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("control_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("authority_protocol", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("kind IN ('linux','android')", name="ck_device_paths_kind"),
        sa.CheckConstraint("ready_count BETWEEN 0 AND 2", name="ck_device_paths_ready"),
        sa.CheckConstraint("authority_protocol IN (0,1)", name="ck_device_paths_protocol"),
        sa.CheckConstraint("row_version > 0", name="ck_device_paths_version"),
    )
    op.create_index("ix_device_paths_terminal", "device_paths", ["terminal_id", "device_id"])
    op.create_table(
        "device_authorities",
        sa.Column(
            "device_id",
            sa.Uuid(),
            sa.ForeignKey("target_devices.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "holder_terminal_id", sa.Uuid(), sa.ForeignKey("terminals.id", ondelete="RESTRICT")
        ),
        sa.Column("instance_id", sa.Uuid()),
        sa.Column(
            "requested_terminal_id", sa.Uuid(), sa.ForeignKey("terminals.id", ondelete="RESTRICT")
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("require_ocr", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('active','releasing','released')", name="ck_device_authority_state"
        ),
        sa.CheckConstraint(
            "generation > 0 AND row_version > 0", name="ck_device_authority_version"
        ),
    )
    op.create_index(
        "ix_device_authority_holder", "device_authorities", ["holder_terminal_id", "state"]
    )
    # Trusted internal values only. Existing writers keep the current owner; protocol=0
    # prevents automatic takeover until both endpoints support physical release.
    op.execute("""INSERT INTO device_paths(device_id,kind,terminal_id)
        SELECT d.id, CASE t.terminal_type WHEN 'linux' THEN 'linux' ELSE 'android' END,
               d.managing_terminal_id
        FROM target_devices d JOIN terminals t ON t.id=d.managing_terminal_id
        WHERE d.deleted_at IS NULL AND t.deleted_at IS NULL""")
    op.execute("""INSERT INTO device_authorities(device_id,holder_terminal_id,updated_at)
        SELECT id,managing_terminal_id,CURRENT_TIMESTAMP FROM target_devices
        WHERE deleted_at IS NULL AND managing_terminal_id IS NOT NULL""")


def downgrade() -> None:
    # Requires stopping new dispatch and settling all Android authorities first.
    op.drop_table("device_authorities")
    op.drop_table("device_paths")
