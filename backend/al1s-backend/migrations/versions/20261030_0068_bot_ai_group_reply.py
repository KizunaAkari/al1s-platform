"""Allow opt-in AI replies for individual authorized QQ groups."""

import sqlalchemy as sa
from alembic import op

revision = "20261030_0068"
down_revision = "20261030_0067"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_ai_scopes", sa.Column("reply_all", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))


def downgrade():
    raise RuntimeError("bot_ai_group_reply_downgrade_requires_preserving_group_choices")
