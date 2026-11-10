"""Add isolated futures identity and mode bookkeeping without altering spot identity."""

from alembic import op
import sqlalchemy as sa

revision = "20261028_0061"
down_revision = "20261028_0060"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("futures_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("futures_initial_cash", sa.Numeric(20, 4), nullable=False, server_default="1000"),
        sa.Column("futures_fee", sa.Numeric(12, 8), nullable=False, server_default="0.0005"),
        sa.Column("futures_default_leverage", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("futures_max_leverage", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("freqtrade_settings", column)
    op.create_check_constraint(
        "ck_freqtrade_futures_settings",
        "freqtrade_settings",
        "futures_initial_cash > 0 AND futures_fee >= 0 AND futures_fee <= 1 "
        "AND futures_default_leverage BETWEEN 1 AND 150 "
        "AND (futures_max_leverage = 0 OR futures_default_leverage <= futures_max_leverage) "
        "AND futures_max_leverage BETWEEN 0 AND 150",
    )
    op.create_table(
        "freqtrade_futures_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("qq_id", sa.String(20), unique=True, nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id"), nullable=False),
        sa.Column("initial_cash", sa.Numeric(20, 4), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("native_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('ready','unknown') AND initial_cash > 0", name="ck_freqtrade_futures_profile"
        ),
    )
    op.add_column(
        "freqtrade_requests",
        sa.Column("market", sa.String(8), nullable=False, server_default="spot"),
    )
    op.create_check_constraint(
        "ck_freqtrade_request_market", "freqtrade_requests", "market IN ('spot','futures')"
    )
    op.create_index(
        "ix_freqtrade_request_market_owner",
        "freqtrade_requests",
        ["status", "market", "sender_id", "created_at", "id"],
    )


def downgrade():
    raise RuntimeError("futures_downgrade_requires_explicit_data_migration")
