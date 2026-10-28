"""Coalesced daily/manual Android setup checks with actual completion facts."""

import sqlalchemy as sa
from alembic import op

revision = "20261006_0054"
down_revision = "20261006_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "terminal_setup_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "terminal_id",
            sa.Uuid(),
            sa.ForeignKey("terminals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("instance_id", sa.Uuid()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *[
            sa.Column(name, sa.Boolean())
            for name in (
                "secure_lock",
                "provider_ready",
                "battery_exempt",
                "notifications_allowed",
                "wireless_recovery_enabled",
            )
        ],
        sa.Column("manufacturer_policy", sa.String(32)),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('pending','claimed','completed')", name="ck_setup_check_status"
        ),
        sa.CheckConstraint("source IN ('automatic','manual')", name="ck_setup_check_source"),
        sa.CheckConstraint("row_version > 0", name="ck_setup_check_version"),
    )
    op.create_index(
        "uq_setup_check_pending",
        "terminal_setup_checks",
        ["terminal_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','claimed')"),
    )
    op.create_index(
        "ix_setup_check_completed", "terminal_setup_checks", ["terminal_id", "completed_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("terminal_setup_checks")
