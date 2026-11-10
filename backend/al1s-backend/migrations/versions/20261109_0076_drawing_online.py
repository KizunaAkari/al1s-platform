"""Add versioned online image presets and request states."""

import sqlalchemy as sa
from alembic import op

revision = "20261109_0076"
down_revision = "20261109_0075"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_drawing_presets", sa.Column("engine", sa.String(16), nullable=False, server_default="comfy"))
    op.drop_constraint("ck_drawing_preset_kind", "bot_drawing_presets", type_="check")
    op.create_check_constraint("ck_drawing_preset_kind", "bot_drawing_presets",
        "(engine = 'comfy' AND kind IN ('txt2img','image_edit')) OR (engine = 'openai' AND kind = 'both')")
    op.add_column("bot_drawing_settings", sa.Column("default_online_preset_id", sa.Uuid()))
    op.add_column("bot_drawing_settings", sa.Column("resource_applied_version", sa.Integer(), nullable=False, server_default="0"))
    op.execute(sa.text("UPDATE bot_drawing_settings SET resource_applied_version = applied_version"))
    op.create_check_constraint("ck_drawing_resource_version", "bot_drawing_settings", "resource_applied_version >= 0")
    op.create_foreign_key("fk_drawing_default_online", "bot_drawing_settings", "bot_drawing_presets",
                          ["service_id", "default_online_preset_id"], ["service_id", "id"], ondelete="RESTRICT")
    for name in ("source_graph", "execution_template", "source_bindings", "bindings", "graph_hash", "execution_template_hash"):
        op.alter_column("bot_drawing_preset_revisions", name, nullable=True)
    for name, kind in (("online_model_id", sa.String(200)), ("online_base_url", sa.String(1024)),
                       ("online_network_mode", sa.String(8)), ("online_size", sa.String(16)),
                       ("online_quality", sa.String(8)), ("online_prompt_model", sa.Boolean()),
                       ("online_secret_id", sa.Uuid())):
        op.add_column("bot_drawing_preset_revisions", sa.Column(name, kind))
    op.create_foreign_key("fk_drawing_online_secret", "bot_drawing_preset_revisions", "encrypted_secrets",
                          ["online_secret_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_drawing_online_network", "bot_drawing_preset_revisions",
                               "online_network_mode IS NULL OR online_network_mode IN ('direct','proxy')")
    op.create_check_constraint("ck_drawing_online_config", "bot_drawing_preset_revisions",
        "(online_model_id IS NULL AND online_base_url IS NULL AND online_network_mode IS NULL "
        "AND online_size IS NULL AND online_quality IS NULL AND online_prompt_model IS NULL "
        "AND online_secret_id IS NULL) OR (online_model_id IS NOT NULL AND online_model_id IN "
        "('gpt-image-2.6-flare','gpt-image-2.6-sunburst') AND online_base_url IS NOT NULL "
        "AND online_network_mode IS NOT NULL AND online_network_mode IN ('direct','proxy') "
        "AND online_size IS NOT NULL AND online_size IN "
        "('1024x1024','1024x1536','1536x1024') AND online_quality IS NOT NULL AND online_quality IN "
        "('low','medium','high','xhigh','max') AND online_prompt_model IS NOT NULL)")
    op.add_column("bot_drawing_tasks", sa.Column("online_request_state", sa.String(16)))
    op.add_column("bot_drawing_tasks", sa.Column("online_request_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("bot_drawing_tasks", sa.Column("online_image_received", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.create_check_constraint("ck_drawing_task_online_request", "bot_drawing_tasks",
        "online_request_state IS NULL OR online_request_state IN "
        "('not_started','submitting','sent','received','unknown','failed')")
    op.create_check_constraint("ck_drawing_task_online_facts", "bot_drawing_tasks",
        "(NOT online_image_received OR online_request_sent) AND "
        "(online_request_state IS NOT NULL OR (NOT online_request_sent AND NOT online_image_received))")


def downgrade():
    raise RuntimeError("drawing_online_presets_require_forward_recovery")
