"""Persist drawing prompt profiles, queue stages, and manually retained history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261109_0075"
down_revision = "20261105_0074"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_drawing_prompt_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("normalized_name", sa.String(128), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["service_id"], ["bot_drawing_settings.service_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("service_id", "id", name="uq_drawing_prompt_profile_owner"),
        sa.CheckConstraint("current_revision > 0 AND row_version > 0", name="ck_drawing_prompt_profile_version"),
    )
    op.create_index("uq_drawing_prompt_profile_name", "bot_drawing_prompt_profiles",
                    ["service_id", "normalized_name"], unique=True,
                    postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_table(
        "bot_drawing_prompt_profile_revisions",
        sa.Column("profile_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_kind", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=False, server_default=""),
        sa.Column("network_mode", sa.String(8), nullable=False, server_default="direct"),
        sa.Column("secret_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["service_id", "profile_id"],
            ["bot_drawing_prompt_profiles.service_id", "bot_drawing_prompt_profiles.id"],
            name="fk_drawing_prompt_revision_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["secret_id"], ["encrypted_secrets.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("service_id", "profile_id", "revision", name="uq_drawing_prompt_revision_owner"),
        sa.CheckConstraint("revision > 0 AND network_mode IN ('direct','proxy')", name="ck_drawing_prompt_revision"),
    )
    op.add_column("bot_drawing_settings", sa.Column("active_prompt_profile_id", sa.Uuid()))
    op.add_column("bot_drawing_settings", sa.Column("prompt_selection_version", sa.Integer(),
                                                   nullable=False, server_default="1"))
    op.drop_constraint("ck_drawing_settings_version", "bot_drawing_settings", type_="check")
    op.create_check_constraint("ck_drawing_settings_version", "bot_drawing_settings",
        "next_workflow_number > 0 AND row_version > 0 AND applied_version >= 0 "
        "AND media_bytes >= 0 AND prompt_selection_version > 0")
    op.create_foreign_key("fk_drawing_active_prompt_profile", "bot_drawing_settings", "bot_drawing_prompt_profiles",
                          ["service_id", "active_prompt_profile_id"], ["service_id", "id"], ondelete="RESTRICT")
    op.execute(sa.text("""
        INSERT INTO bot_drawing_prompt_profiles (id, service_id, name, normalized_name, current_revision)
        SELECT gen_random_uuid(), service_id, '原许愿模型', '原许愿模型', 1
        FROM bot_drawing_prompt_providers
    """))
    op.execute(sa.text("""
        INSERT INTO bot_drawing_prompt_profile_revisions
            (profile_id, revision, service_id, adapter_kind, model_id, base_url, network_mode, secret_id)
        SELECT profile.id, 1, provider.service_id, provider.adapter_kind, provider.model_id,
               provider.base_url, provider.network_mode, provider.secret_id
        FROM bot_drawing_prompt_providers AS provider
        JOIN bot_drawing_prompt_profiles AS profile ON profile.service_id = provider.service_id
    """))
    op.execute(sa.text("""
        UPDATE bot_drawing_settings AS settings SET active_prompt_profile_id = profile.id
        FROM bot_drawing_prompt_profiles AS profile WHERE profile.service_id = settings.service_id
    """))
    op.drop_table("bot_drawing_prompt_providers")

    op.add_column("bot_drawing_tasks", sa.Column("wish_text", sa.Text()))
    op.add_column("bot_drawing_tasks", sa.Column("prompt_workflow_name", sa.String(32)))
    op.add_column("bot_drawing_tasks", sa.Column("prompt_aliases", JSONB()))
    op.add_column("bot_drawing_tasks", sa.Column("prompt_profile_id", sa.Uuid()))
    op.add_column("bot_drawing_tasks", sa.Column("prompt_profile_revision", sa.Integer()))
    op.add_column("bot_drawing_tasks", sa.Column("prompt_status", sa.String(16), nullable=False, server_default="ready"))
    op.add_column("bot_drawing_tasks", sa.Column("processing_stage", sa.String(32), nullable=False, server_default="queued"))
    op.add_column("bot_drawing_tasks", sa.Column("cancel_requested", sa.Boolean(), nullable=False,
                                                server_default=sa.text("false")))
    op.add_column("bot_drawing_tasks", sa.Column("deletion_state", sa.String(16), nullable=False, server_default="visible"))
    op.add_column("bot_drawing_tasks", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("bot_drawing_tasks", sa.Column("comfy_endpoint", sa.String(1024)))
    op.create_foreign_key("fk_drawing_task_prompt_revision", "bot_drawing_tasks",
        "bot_drawing_prompt_profile_revisions", ["service_id", "prompt_profile_id", "prompt_profile_revision"],
        ["service_id", "profile_id", "revision"], ondelete="RESTRICT")
    op.create_check_constraint("ck_drawing_task_prompt_status", "bot_drawing_tasks",
                               "prompt_status IN ('waiting','working','ready','failed')")
    op.create_check_constraint("ck_drawing_task_prompt_revision", "bot_drawing_tasks",
                               "(prompt_profile_id IS NULL) = (prompt_profile_revision IS NULL)")
    op.create_check_constraint("ck_drawing_task_deletion", "bot_drawing_tasks",
                               "deletion_state IN ('visible','deleting','deleted')")
    op.create_index("ix_drawing_task_history", "bot_drawing_tasks",
                    ["service_id", "deletion_state", "created_at", "id"])
    op.create_index("ix_drawing_task_deleting", "bot_drawing_tasks", ["id"],
                    postgresql_where=sa.text("deletion_state = 'deleting'"))
    op.execute(sa.text("""
        UPDATE bot_drawing_tasks SET processing_stage = CASE
            WHEN generation_status = 'running' THEN 'executing'
            WHEN generation_status = 'submitting' THEN 'preparing_resources'
            WHEN generation_status = 'cancelling' THEN 'cancelling'
            WHEN generation_status = 'reconciling' THEN 'reconciling'
            WHEN generation_status = 'queued' THEN 'queued'
            WHEN send_status IN ('ready', 'sending') THEN 'sending'
            WHEN cleanup_status IN ('pending', 'deferred', 'failed') THEN 'cleaning'
            WHEN send_status = 'unknown' THEN 'delivery_unknown'
            ELSE 'finished' END
    """))


def downgrade():
    raise RuntimeError("drawing_pipeline_profiles_and_history_require_forward_recovery")
