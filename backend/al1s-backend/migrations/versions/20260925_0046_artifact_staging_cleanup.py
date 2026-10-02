"""Schedule bounded physical cleanup of terminal artifact staging objects."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0046"
down_revision = "20260925_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "terminal_artifacts",
        sa.Column("staging_cleanup_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_terminal_artifacts_staging_cleanup",
        "terminal_artifacts",
        ["staging_cleanup_at", "id"],
    )
    op.execute(
        "UPDATE terminal_artifacts SET staging_cleanup_at = "
        "GREATEST(expires_at, completed_at) + INTERVAL '2 hours' "
        "WHERE status IN ('ready', 'expired')"
    )


def downgrade() -> None:
    op.drop_index("ix_terminal_artifacts_staging_cleanup", table_name="terminal_artifacts")
    op.drop_column("terminal_artifacts", "staging_cleanup_at")
