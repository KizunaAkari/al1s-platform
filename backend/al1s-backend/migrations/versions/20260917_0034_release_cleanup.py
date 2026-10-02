"""Bounded cleanup of published release upload copies, not immutable objects."""

import sqlalchemy as sa
from alembic import op

revision = "20260917_0034"
down_revision = "20260917_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("linux_releases", sa.Column("staging_cleanup_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE linux_releases SET staging_cleanup_at = published_at + interval '2 hours' "
        "WHERE state = 'published'"
    )
    op.create_index("ix_linux_release_cleanup", "linux_releases", ["staging_cleanup_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_linux_release_cleanup", table_name="linux_releases")
    op.drop_column("linux_releases", "staging_cleanup_at")
