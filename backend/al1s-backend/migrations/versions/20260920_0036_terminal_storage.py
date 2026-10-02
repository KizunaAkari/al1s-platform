"""Keep one current filesystem observation and durable alert latch per terminal."""

import sqlalchemy as sa
from alembic import op

revision = "20260920_0036"
down_revision = "20260920_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("terminals", sa.Column("storage_directory", sa.String(1024)))
    for name in ("storage_total_bytes", "storage_used_bytes", "storage_available_bytes"):
        op.add_column("terminals", sa.Column(name, sa.BigInteger()))
    op.add_column("terminals", sa.Column("storage_observed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "terminals",
        sa.Column("storage_probe_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "terminals",
        sa.Column("storage_alert_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_terminals_storage",
        "terminals",
        "storage_total_bytes IS NULL OR (storage_total_bytes > 0 AND storage_used_bytes >= 0 "
        "AND storage_available_bytes >= 0 AND "
        "storage_used_bytes + storage_available_bytes <= storage_total_bytes)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_terminals_storage", "terminals", type_="check")
    for name in (
        "storage_alert_active",
        "storage_probe_ok",
        "storage_observed_at",
        "storage_available_bytes",
        "storage_used_bytes",
        "storage_total_bytes",
        "storage_directory",
    ):
        op.drop_column("terminals", name)
