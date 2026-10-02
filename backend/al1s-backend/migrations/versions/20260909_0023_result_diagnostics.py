"""Preserve structured formal execution diagnostics separately from rejection summaries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260909_0023"
down_revision = "20260909_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("terminal_reports", sa.Column("result_diagnostic", postgresql.JSONB()))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM terminal_reports WHERE result_diagnostic IS NOT NULL "
        "AND result_diagnostic <> 'null'::jsonb)"
    )).scalar():
        raise RuntimeError("Cannot discard retained execution diagnostics")
    op.drop_column("terminal_reports", "result_diagnostic")
