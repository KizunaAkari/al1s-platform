"""QQ identity and request bookkeeping; Freqtrade owns its separate native ledger."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261028_0060"
down_revision = "20261028_0059"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "freqtrade_settings",
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("private_enabled", sa.Boolean(), nullable=False),
        sa.Column("event_ws_url", sa.String(1024)),
        sa.Column("groups", postgresql.ARRAY(sa.String(20)), nullable=False),
        sa.Column("initial_cash", sa.Numeric(20, 4), nullable=False),
        sa.Column("fee", sa.Numeric(12, 8), nullable=False),
        sa.Column("reply_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("max_active_accounts", sa.Integer(), nullable=False),
        sa.Column("idle_seconds", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "initial_cash > 0 AND row_version > 0 AND fee >= 0 AND fee <= 1",
            name="ck_freqtrade_settings",
        ),
    )
    op.create_table(
        "freqtrade_commands",
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("freqtrade_settings.service_id"),
            primary_key=True,
        ),
        sa.Column("action", sa.String(16), primary_key=True),
        sa.Column("keyword", sa.String(32), nullable=False),
        sa.Column("normalized_keyword", sa.String(64), nullable=False),
        sa.UniqueConstraint("service_id", "normalized_keyword", name="uq_freqtrade_word"),
    )
    op.create_table(
        "freqtrade_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("qq_id", sa.String(20), nullable=False, unique=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id"), nullable=False),
        sa.Column("initial_cash", sa.Numeric(20, 4), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("native_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('ready','unknown') AND initial_cash > 0", name="ck_freqtrade_profile"
        ),
    )
    op.create_table(
        "freqtrade_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id"), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(20), nullable=False),
        sa.Column("sender_id", sa.String(20), nullable=False),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("event_time", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.Text()),
        sa.Column("native_trade_id", sa.Integer()),
        sa.Column("native_order_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_freqtrade_event",
        ),
        sa.CheckConstraint(
            "status IN ('processing','completed','unknown')", name="ck_freqtrade_request"
        ),
    )
    op.create_index(
        "ix_freqtrade_request_owner", "freqtrade_requests", ["sender_id", "created_at", "id"]
    )


def downgrade():
    raise RuntimeError("freqtrade_downgrade_requires_explicit_data_migration")
