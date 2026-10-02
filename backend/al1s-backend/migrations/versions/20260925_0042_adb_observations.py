"""Record ADB observations without treating terminal heartbeat as phone presence."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0042"
down_revision = "20260925_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_device_identifiers", sa.Column("adb_state", sa.String(24)))
    op.add_column(
        "target_device_identifiers", sa.Column("observed_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_target_identifiers_adb_state", "target_device_identifiers",
        "adb_state IS NULL OR (source_type = 'adb_serial' "
        "AND adb_state IN ('device','offline','unauthorized','other'))",
    )
    op.create_index(
        "ix_target_identifiers_presence", "target_device_identifiers",
        ["target_device_id", "observed_at"],
        postgresql_where=sa.text("deleted_at IS NULL AND source_type = 'adb_serial'"),
    )


def downgrade() -> None:
    op.drop_index("ix_target_identifiers_presence", table_name="target_device_identifiers")
    op.drop_constraint(
        "ck_target_identifiers_adb_state", "target_device_identifiers", type_="check"
    )
    op.drop_column("target_device_identifiers", "observed_at")
    op.drop_column("target_device_identifiers", "adb_state")
