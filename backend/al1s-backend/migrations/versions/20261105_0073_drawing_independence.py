"""Give QQ drawing its own access and prompt-model configuration."""

import sqlalchemy as sa
from alembic import op

revision = "20261105_0073"
down_revision = "20261105_0072"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_drawing_settings", sa.Column("allow_private", sa.Boolean(),
                  nullable=False, server_default=sa.text("false")))
    op.add_column("bot_drawing_settings", sa.Column("event_ws_url", sa.String(1024)))
    op.add_column("bot_drawing_settings", sa.Column("comfy_base_url", sa.String(1024),
                  nullable=False, server_default="http://host.docker.internal:8188"))
    op.create_table("bot_drawing_scopes",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.String(20), nullable=False),
        sa.PrimaryKeyConstraint("service_id", "group_id"),
        sa.ForeignKeyConstraint(["service_id"], ["bot_drawing_settings.service_id"], ondelete="CASCADE"))
    op.create_table("bot_drawing_prompt_providers",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_kind", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=False, server_default=""),
        sa.Column("network_mode", sa.String(8), nullable=False, server_default="direct"),
        sa.Column("secret_id", sa.Uuid()),
        sa.PrimaryKeyConstraint("service_id"),
        sa.ForeignKeyConstraint(["service_id"], ["bot_drawing_settings.service_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["secret_id"], ["encrypted_secrets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("network_mode IN ('direct','proxy')", name="ck_drawing_prompt_network"))
    op.execute(sa.text("""
        INSERT INTO bot_drawing_settings (service_id, enabled)
        SELECT service_id, true FROM bot_ai_settings
        ON CONFLICT (service_id) DO NOTHING
    """))
    op.execute(sa.text("""
        UPDATE bot_drawing_settings AS drawing
        SET event_ws_url = ai.event_ws_url, allow_private = ai.allow_private
        FROM bot_ai_settings AS ai WHERE ai.service_id = drawing.service_id
    """))
    op.execute(sa.text("""
        UPDATE bot_drawing_settings AS drawing SET comfy_base_url = provider.base_url
        FROM bot_ai_providers AS provider
        WHERE provider.service_id = drawing.service_id AND provider.role = 'vision'
          AND provider.adapter_kind = 'al1s_comfyui_vision' AND provider.base_url <> ''
    """))


def downgrade():
    raise RuntimeError("drawing_independent_settings_require_forward_recovery")
