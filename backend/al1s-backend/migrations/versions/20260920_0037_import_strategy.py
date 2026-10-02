"""Track the strategy created by an archive import for replay and reimport."""

import sqlalchemy as sa
from alembic import op

revision = "20260920_0037"
down_revision = "20260920_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("maa_import_batches", sa.Column("strategy_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_maa_import_strategy", "maa_import_batches", "maa_strategies",
                          ["strategy_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    op.drop_constraint("fk_maa_import_strategy", "maa_import_batches", type_="foreignkey")
    op.drop_column("maa_import_batches", "strategy_id")
