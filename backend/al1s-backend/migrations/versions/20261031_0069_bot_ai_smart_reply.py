"""Persist opt-in smart group replies and presentation choices."""

import sqlalchemy as sa
from alembic import op

revision = "20261031_0069"
down_revision = "20261030_0068"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_ai_scopes", sa.Column("smart_reply", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    op.create_check_constraint("ck_bot_ai_reply_mode", "bot_ai_scopes",
                               "NOT (reply_all AND smart_reply)")
    for column in (
        sa.Column("smart_style", sa.String(16), nullable=False, server_default="context"),
        sa.Column("smart_min_interval_seconds", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("smart_cooldown_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("smart_window_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("smart_max_replies", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("smart_voice_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("smart_stickers_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("smart_collect_stickers", sa.Boolean(), nullable=False, server_default=sa.true()),
    ):
        op.add_column("bot_ai_settings", column)
    op.create_check_constraint("ck_bot_ai_smart_style", "bot_ai_settings",
                               "smart_style IN ('context','random')")
    op.create_check_constraint("ck_bot_ai_smart_limits", "bot_ai_settings",
        "smart_min_interval_seconds BETWEEN 0 AND 3600 AND "
        "smart_cooldown_seconds BETWEEN 0 AND 3600 AND "
        "smart_window_seconds BETWEEN 1 AND 3600 AND smart_max_replies BETWEEN 1 AND 50")


def downgrade():
    raise RuntimeError("bot_ai_smart_reply_downgrade_requires_preserving_group_choices")
