"""Bind Android terminal registration grants to logical target devices.

Revision ID: 20260905_0018
Revises: 20260901_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260905_0018"
down_revision: str | None = "20260901_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "terminal_registration_grants",
        sa.Column("target_device_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_terminal_registration_grants_target_device",
        "terminal_registration_grants",
        "target_devices",
        ["target_device_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_terminal_registration_grants_target_type",
        "terminal_registration_grants",
        "target_device_id IS NULL OR allowed_terminal_type = 'android'",
    )
    op.create_index(
        "uq_target_identifiers_active_apk_target",
        "target_device_identifiers",
        ["target_device_id"],
        unique=True,
        postgresql_where=sa.text(
            "deleted_at IS NULL AND source_type = 'apk_installation' "
            "AND target_device_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_target_identifiers_active_apk_target",
        table_name="target_device_identifiers",
    )
    op.drop_constraint(
        "ck_terminal_registration_grants_target_type",
        "terminal_registration_grants",
        type_="check",
    )
    op.drop_constraint(
        "fk_terminal_registration_grants_target_device",
        "terminal_registration_grants",
        type_="foreignkey",
    )
    op.drop_column("terminal_registration_grants", "target_device_id")
