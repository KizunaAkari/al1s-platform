"""Add per-session QQ AI chat profiles without replacing existing credentials."""

import sqlalchemy as sa
from alembic import op

revision = "20261030_0066"
down_revision = "20261029_0065"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_ai_chat_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_ai_settings.service_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("normalized_name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("adapter_kind", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=False),
        sa.Column("secret_id", sa.Uuid(), sa.ForeignKey("encrypted_secrets.id")),
        sa.Column("supports_vision", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("service_id", "id", name="uq_bot_ai_chat_profile_service_id"),
        sa.UniqueConstraint("service_id", "normalized_name", name="uq_bot_ai_chat_profile_name"),
    )
    op.create_index("uq_bot_ai_chat_default", "bot_ai_chat_profiles", ["service_id"], unique=True,
                    postgresql_where=sa.text("is_default"))
    op.create_table(
        "bot_ai_session_profiles",
        sa.Column("service_id", sa.Uuid(), primary_key=True),
        sa.Column("scope_kind", sa.String(16), primary_key=True),
        sa.Column("target_id", sa.String(20), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["service_id", "profile_id"],
            ["bot_ai_chat_profiles.service_id", "bot_ai_chat_profiles.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("scope_kind IN ('qq_group','qq_private')", name="ck_bot_ai_session_profile_scope"),
    )
    op.create_index("ix_bot_ai_session_profile", "bot_ai_session_profiles", ["service_id", "profile_id"])
    op.execute("""
        INSERT INTO bot_ai_chat_profiles
            (id, service_id, name, normalized_name, description, persona, adapter_kind,
             model_id, base_url, secret_id, supports_vision, is_default)
        SELECT gen_random_uuid(), p.service_id,
            CASE WHEN lower(rtrim(p.base_url, '/')) IN
                ('https://api.deepseek.com', 'https://api.deepseek.com/v1')
                THEN '大肥鱼' ELSE '默认人格' END,
            CASE WHEN lower(rtrim(p.base_url, '/')) IN
                ('https://api.deepseek.com', 'https://api.deepseek.com/v1')
                THEN '大肥鱼' ELSE '默认人格' END,
            '', s.persona, p.adapter_kind, p.model_id, p.base_url, p.secret_id,
            s.vision_enabled AND NOT EXISTS (
                SELECT 1 FROM bot_ai_providers v WHERE v.service_id=p.service_id
                    AND v.role='vision' AND v.adapter_kind<>'follow_chat'
            ), TRUE
        FROM bot_ai_providers p JOIN bot_ai_settings s ON s.service_id=p.service_id
        WHERE p.role='chat'
    """)
    op.execute("""
        UPDATE bot_ai_settings
        SET desired_version = desired_version + 1, last_error = NULL
        WHERE EXISTS (SELECT 1 FROM bot_ai_chat_profiles p
                      WHERE p.service_id = bot_ai_settings.service_id)
    """)


def downgrade():
    raise RuntimeError("bot_ai_personas_downgrade_requires_preserving_profiles_and_selections")
