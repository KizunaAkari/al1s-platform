"""Establish the new platform migration baseline.

Revision ID: 20260828_0001
Revises:
Create Date: 2026-08-28
"""

from collections.abc import Sequence

revision: str = "20260828_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The first business tables are intentionally deferred to stage 2."""


def downgrade() -> None:
    """Return to the empty migration base."""
