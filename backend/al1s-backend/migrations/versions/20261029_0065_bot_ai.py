"""Add isolated QQ AI configuration and bounded request bookkeeping."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261029_0065"
down_revision = "20261029_0064"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_ai_settings",
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("event_ws_url", sa.String(1024)),
        sa.Column("allow_private", sa.Boolean(), nullable=False),
        sa.Column("vision_enabled", sa.Boolean(), nullable=False),
        sa.Column("stt_enabled", sa.Boolean(), nullable=False),
        sa.Column("tts_mode", sa.String(16), nullable=False),
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("wake_words", JSONB(), nullable=False),
        sa.Column("desired_version", sa.Integer(), nullable=False),
        sa.Column("applied_version", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(64)),
        sa.CheckConstraint("desired_version > 0 AND applied_version >= 0", name="ck_bot_ai_version"),
        sa.CheckConstraint("tts_mode IN ('off','voice_input','always')", name="ck_bot_ai_tts_mode"),
    )
    op.create_table(
        "bot_ai_scopes",
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_ai_settings.service_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("scope_kind", sa.String(16), primary_key=True),
        sa.Column("target_id", sa.String(20), primary_key=True),
        sa.CheckConstraint("scope_kind = 'qq_group'", name="ck_bot_ai_scope"),
    )
    op.create_table(
        "bot_ai_providers",
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_ai_settings.service_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), primary_key=True),
        sa.Column("adapter_kind", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=False),
        sa.Column("secret_id", sa.Uuid(), sa.ForeignKey("encrypted_secrets.id")),
        sa.Column("voice", sa.String(200), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.CheckConstraint("role IN ('chat','vision','stt','tts')", name="ck_bot_ai_provider_role"),
    )
    op.create_table(
        "bot_ai_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id"), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(20), nullable=False),
        sa.Column("sender_id", sa.String(20), nullable=False),
        sa.Column("source_message_id", sa.String(64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("send_status", sa.String(16), nullable=False),
        sa.Column("epoch", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_code", sa.String(64)),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("meme_receipt_id", sa.Uuid(), sa.ForeignKey("meme_command_receipts.id")),
        sa.UniqueConstraint("service_id", "source_message_id", name="uq_bot_ai_source_message"),
        sa.CheckConstraint("scope_kind IN ('qq_group','qq_private')", name="ck_bot_ai_request_scope"),
        sa.CheckConstraint("status IN ('queued','running','stopping','blocked','completed','failed','expired','interrupted','unknown')", name="ck_bot_ai_request_status"),
        sa.CheckConstraint("send_status IN ('none','ready','sending','sent','failed','unknown')", name="ck_bot_ai_send_status"),
        sa.CheckConstraint("sent_count BETWEEN 0 AND 5", name="ck_bot_ai_sent_count"),
    )
    op.create_index("ix_bot_ai_expiry", "bot_ai_requests", ["status", "expires_at", "id"])
    op.create_index("ix_bot_ai_service_time", "bot_ai_requests", ["service_id", "created_at", "id"])
    op.create_index("uq_bot_ai_active_session", "bot_ai_requests",
                    ["service_id", "scope_kind", "target_id"], unique=True,
                    postgresql_where=sa.text("status IN ('running','stopping','blocked')"))


def downgrade():
    raise RuntimeError("bot_ai_downgrade_requires_preserving_configuration_and_receipts")
