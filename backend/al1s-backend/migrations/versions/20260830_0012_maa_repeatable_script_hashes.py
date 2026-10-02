"""Allow a move to create a fresh qualification generation for the same content hash.

Revision ID: 20260830_0012
Revises: 20260830_0011
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260830_0012"
down_revision: str | None = "20260830_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_maa_script_versions_hash",
        "maa_script_versions",
        type_="unique",
    )
    op.create_index(
        "ix_maa_script_versions_hash",
        "maa_script_versions",
        ["script_id", "manifest_hash", "revision"],
    )


def downgrade() -> None:
    op.drop_index("ix_maa_script_versions_hash", table_name="maa_script_versions")
    op.create_unique_constraint(
        "uq_maa_script_versions_hash",
        "maa_script_versions",
        ["script_id", "manifest_hash"],
    )
