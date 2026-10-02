"""Persist platform maintenance commands independently of browser sessions."""
import sqlalchemy as sa
from alembic import op

revision = "20260916_0031"
down_revision = "20260916_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maa_maintenance_commands",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("terminal_id", sa.Uuid(), sa.ForeignKey("terminals.id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("remote_version", sa.Integer(), nullable=False),
        sa.Column("late_state", sa.String(32)),
        sa.Column("late_observed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("action IN ('restart_container','restart_host')",
                           name="ck_maa_maintenance_action"),
        sa.CheckConstraint("state IN ('pending','accepted','executing','recovering','succeeded',"
                           "'refused','expired','response_timeout','recovery_timeout')",
                           name="ck_maa_maintenance_state"),
    )
    op.create_index("ix_maa_maintenance_poll", "maa_maintenance_commands", ["next_check_at", "id"])
    op.create_index("ix_maa_maintenance_terminal", "maa_maintenance_commands",
                    ["terminal_id", "submitted_at", "id"])
    op.create_index("uq_maa_maintenance_active", "maa_maintenance_commands",
                    ["terminal_id"], unique=True,
                    postgresql_where=sa.text(
                        "state IN ('pending','accepted','executing','recovering')"))


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM maa_maintenance_commands"))
    if count:
        raise RuntimeError("Maintenance records exist; refusing destructive downgrade")
    op.drop_table("maa_maintenance_commands")
