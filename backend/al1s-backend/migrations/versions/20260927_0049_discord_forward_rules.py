"""Persist typed Discord forwarding triggers and destination actions."""

import sqlalchemy as sa
from alembic import op

revision = "20260927_0049"
down_revision = "20260926_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discord_forward_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_service_id",
            sa.Uuid(),
            sa.ForeignKey("bot_services.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(32), nullable=False),
        sa.Column("trigger_kind", sa.String(20), nullable=False),
        sa.Column("trigger_text", sa.String(256)),
        sa.Column("frequency_count", sa.Integer()),
        sa.Column("frequency_window_seconds", sa.Integer()),
        sa.Column("cooldown_seconds", sa.Integer()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "trigger_kind IN ('contains','acrostic','frequency')", name="ck_discord_forward_trigger"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_discord_forward_version"),
        sa.CheckConstraint(
            "frequency_count IS NULL OR frequency_count BETWEEN 2 AND 64",
            name="ck_discord_forward_count",
        ),
        sa.CheckConstraint(
            "frequency_window_seconds IS NULL OR frequency_window_seconds BETWEEN 1 AND 3600",
            name="ck_discord_forward_window",
        ),
        sa.CheckConstraint(
            "cooldown_seconds IS NULL OR cooldown_seconds BETWEEN 0 AND 3600",
            name="ck_discord_forward_cooldown",
        ),
    )
    op.create_index(
        "ix_discord_forward_service",
        "discord_forward_rules",
        ["bot_service_id", "created_at", "id"],
    )
    op.create_index(
        "ix_discord_forward_source",
        "discord_forward_rules",
        ["bot_service_id", "guild_id", "channel_id"],
    )
    op.create_table(
        "discord_forward_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "rule_id",
            sa.Uuid(),
            sa.ForeignKey("discord_forward_rules.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "channel_id",
            sa.Uuid(),
            sa.ForeignKey("notification_channels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("review_policy", sa.String(16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('qq_private','qq_group','smtp')", name="ck_discord_forward_action_kind"
        ),
        sa.CheckConstraint(
            "review_policy IN ('none','lexicon')", name="ck_discord_forward_action_policy"
        ),
        sa.CheckConstraint(
            "kind <> 'qq_group' OR review_policy = 'lexicon'",
            name="ck_discord_forward_group_review",
        ),
        sa.CheckConstraint(
            "kind <> 'smtp' OR review_policy = 'none'", name="ck_discord_forward_smtp_review"
        ),
    )
    op.create_index(
        "ix_discord_forward_action_rule", "discord_forward_actions", ["rule_id", "position", "id"]
    )
    op.create_index("ix_discord_forward_action_channel", "discord_forward_actions", ["channel_id"])


def downgrade() -> None:
    op.drop_index("ix_discord_forward_action_channel", table_name="discord_forward_actions")
    op.drop_index("ix_discord_forward_action_rule", table_name="discord_forward_actions")
    op.drop_table("discord_forward_actions")
    op.drop_index("ix_discord_forward_source", table_name="discord_forward_rules")
    op.drop_index("ix_discord_forward_service", table_name="discord_forward_rules")
    op.drop_table("discord_forward_rules")
