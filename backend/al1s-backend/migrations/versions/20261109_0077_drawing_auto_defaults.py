"""Allow automatic size and quality in online drawing presets."""

from alembic import op

revision = "20261109_0077"
down_revision = "20261109_0076"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_drawing_online_config", "bot_drawing_preset_revisions", type_="check")
    op.create_check_constraint("ck_drawing_online_config", "bot_drawing_preset_revisions",
        "(online_model_id IS NULL AND online_base_url IS NULL AND online_network_mode IS NULL "
        "AND online_size IS NULL AND online_quality IS NULL AND online_prompt_model IS NULL "
        "AND online_secret_id IS NULL) OR (online_model_id IS NOT NULL AND online_model_id IN "
        "('gpt-image-2.6-flare','gpt-image-2.6-sunburst') AND online_base_url IS NOT NULL "
        "AND online_network_mode IS NOT NULL AND online_network_mode IN ('direct','proxy') "
        "AND online_size IS NOT NULL AND online_size IN "
        "('auto','1024x1024','1024x1536','1536x1024') AND online_quality IS NOT NULL AND online_quality IN "
        "('auto','low','medium','high','xhigh','max') AND online_prompt_model IS NOT NULL)")


def downgrade():
    raise RuntimeError("drawing_online_auto_requires_forward_recovery")
