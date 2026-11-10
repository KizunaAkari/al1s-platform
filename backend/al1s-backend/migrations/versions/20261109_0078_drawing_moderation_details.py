"""Persist optional provider moderation details with drawing tasks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261109_0078"
down_revision = "20261109_0077"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_drawing_tasks", sa.Column("online_moderation_details", JSONB()))


def downgrade():
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM bot_drawing_tasks WHERE online_moderation_details IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("drawing_moderation_details_require_forward_recovery")
    op.drop_column("bot_drawing_tasks", "online_moderation_details")
