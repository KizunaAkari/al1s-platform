"""Store the administrator's paper-trading binding and operation receipts."""

import sqlalchemy as sa
from alembic import op
revision = "20261110_0079"
down_revision = "20261109_0078"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("trading_admin_owner",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("qq_id", sa.String(20), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1 AND row_version > 0", name="ck_trading_admin_owner"))
    op.create_table("trading_admin_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("qq_id", sa.String(20), nullable=False),
        sa.Column("market", sa.String(12), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dispatch_started", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("account_protected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("protected_version", sa.Integer()),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("result", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("service_id", "key_hash", name="uq_trading_admin_operation_key"),
        sa.CheckConstraint("market IN ('a-share','spot','futures')", name="ck_trading_admin_market"),
        sa.CheckConstraint("status IN ('running','completed','failed','unknown')", name="ck_trading_admin_status"))
    op.create_index("ix_trading_admin_owner_time", "trading_admin_operations", ["qq_id", "created_at", "id"])
    op.create_index("uq_trading_admin_pending", "trading_admin_operations", ["qq_id", "market"], unique=True,
                    postgresql_where=sa.text("status IN ('running','unknown')"))


def downgrade():
    bind = op.get_bind()
    if (bind.execute(sa.text("SELECT 1 FROM trading_admin_owner LIMIT 1")).first()
            or bind.execute(sa.text("SELECT 1 FROM trading_admin_operations LIMIT 1")).first()):
        raise RuntimeError("trading_admin_requires_forward_recovery")
    op.drop_index("uq_trading_admin_pending", table_name="trading_admin_operations")
    op.drop_index("ix_trading_admin_owner_time", table_name="trading_admin_operations")
    op.drop_table("trading_admin_operations")
    op.drop_table("trading_admin_owner")
