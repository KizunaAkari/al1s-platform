"""Add AL1S-owned QQ chat history and outstanding local vision jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261101_0070"
down_revision = "20261031_0069"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_ai_conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(20), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("next_turn", sa.Integer(), nullable=False),
        sa.Column("legacy_id", sa.String(128)),
        sa.Column("legacy_create_pending", sa.Boolean(), nullable=False),
        sa.Column("import_digest", sa.String(64)),
        sa.Column("title", sa.Text()),
        sa.Column("persona_id", sa.String(128)),
        sa.Column("token_usage", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("scope_kind IN ('qq_group','qq_private')", name="ck_ai_history_scope"),
        sa.CheckConstraint("revision > 0 AND next_turn > 0", name="ck_ai_history_order"),
        sa.CheckConstraint("deleted_at IS NULL OR NOT current", name="ck_ai_history_deleted"),
        sa.UniqueConstraint("service_id", "legacy_id", name="uq_ai_history_legacy"),
    )
    op.create_index("uq_ai_history_current", "bot_ai_conversations",
                    ["service_id", "scope_kind", "target_id"], unique=True,
                    postgresql_where=sa.text("current"))
    op.create_index("ix_ai_history_export", "bot_ai_conversations", ["service_id", "id"])
    op.create_table(
        "bot_ai_turns",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("bot_ai_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_ai_turn_order"),
        sa.CheckConstraint("sequence > 0", name="ck_ai_turn_order"),
    )
    op.create_table(
        "bot_ai_messages",
        sa.Column("turn_id", sa.Uuid(), sa.ForeignKey("bot_ai_turns.request_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_ai_message_order"),
        sa.CheckConstraint("role IN ('user','assistant','tool','system')", name="ck_ai_message_role"),
    )
    op.create_table(
        "bot_ai_vision_jobs",
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("endpoint", sa.String(1024), primary_key=True),
        sa.Column("prompt_id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("bot_ai_history_downgrade_requires_preserving_conversations")
