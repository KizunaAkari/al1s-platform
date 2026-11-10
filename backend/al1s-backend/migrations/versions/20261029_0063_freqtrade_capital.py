"""Preserve original capital request amount for native operation recovery."""

from alembic import op
import sqlalchemy as sa

revision = "20261029_0063"
down_revision = "20261029_0062"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "freqtrade_requests", sa.Column("capital_amount", sa.Numeric(20, 4), nullable=True)
    )
    op.create_check_constraint(
        "ck_freqtrade_capital_amount",
        "freqtrade_requests",
        "capital_amount IS NULL OR (capital_amount > 0 AND capital_amount <= 1000000000)",
    )


def downgrade():
    raise RuntimeError("capital_downgrade_requires_preserving_native_funding_state")
