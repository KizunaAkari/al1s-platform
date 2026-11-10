"""Per-command text overrides, independent of existing trading ledgers."""

import sqlalchemy as sa
from alembic import op

revision = "20261029_0064"
down_revision = "20261029_0063"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trading_reply_templates",
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("bot_services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("engine", sa.String(16), primary_key=True),
        sa.Column("action", sa.String(16), primary_key=True),
        sa.Column("variant", sa.String(32), primary_key=True),
        sa.Column("template", sa.Text(), nullable=False),
        sa.CheckConstraint("engine IN ('quant','freqtrade')", name="ck_trading_reply_engine"),
        sa.CheckConstraint("length(template) BETWEEN 1 AND 4096", name="ck_trading_reply_length"),
    )
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='al1s_quant') THEN
        GRANT SELECT ON trading_reply_templates TO al1s_quant;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='al1s_freqtrade') THEN
        GRANT SELECT ON trading_reply_templates TO al1s_freqtrade;
      END IF;
    END $$""")


def downgrade():
    raise RuntimeError("reply_templates_downgrade_requires_preserving_customization")
