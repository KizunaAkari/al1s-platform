"""Record ordered quantitative sources and restricted encrypted credentials."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261028_0059"
down_revision = "20261027_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("quant_settings", "quant_orders"):
        op.add_column(table, sa.Column("provider_order", postgresql.JSONB(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table} SET provider_order = jsonb_build_array(provider) || "
                '(\'["sina","tencent","eastmoney","tushare","gugudata"]\'::jsonb - provider)'
            )
        )
        op.alter_column(table, "provider_order", nullable=False)
    op.add_column("quant_orders", sa.Column("execution_provider", sa.String(32), nullable=True))
    op.execute(
        sa.text(
            "UPDATE quant_orders SET execution_provider = provider WHERE status IN ('processing','partially_filled','filled','unknown')"
        )
    )
    op.create_table(
        "quant_provider_credentials",
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('tushare','gugudata')", name="ck_quant_credential_provider"
        ),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    raise RuntimeError("quant_source_downgrade_requires_data_migration")
