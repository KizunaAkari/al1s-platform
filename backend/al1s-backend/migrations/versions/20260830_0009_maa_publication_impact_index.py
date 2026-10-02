"""Index active task content identities used by Maa publication impact previews.

Revision ID: 20260830_0009
Revises: 20260830_0008
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0009"
down_revision: str | None = "20260830_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_task_requests_content_schedule",
        "task_requests",
        ["source_module", "logical_content_id", "lifecycle_status"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_requests_content_schedule",
        table_name="task_requests",
    )
