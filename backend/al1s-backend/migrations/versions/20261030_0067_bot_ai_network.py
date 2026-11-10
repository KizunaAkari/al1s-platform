"""Store per-persona AI network routing without changing existing selections."""

import sqlalchemy as sa
from alembic import op

revision = "20261030_0067"
down_revision = "20261030_0066"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_ai_chat_profiles", sa.Column("network_mode", sa.String(8),
                  nullable=False, server_default="direct"))
    op.create_check_constraint("ck_bot_ai_chat_profile_network_mode", "bot_ai_chat_profiles",
                               "network_mode IN ('direct','proxy')")


def downgrade():
    raise RuntimeError("bot_ai_network_downgrade_requires_preserving_model_routes")
