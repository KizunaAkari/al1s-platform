"""Persist the two existing meme send gates' configurable intervals."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0056"
down_revision = "20261008_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meme_send_gates",
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "meme_send_gates",
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    # Two fixed platform keys already exist; preserve available_at and the send owner.
    op.execute("UPDATE meme_send_gates SET interval_seconds = 5 WHERE platform = 'discord'")
    op.create_check_constraint(
        "ck_meme_gate_interval", "meme_send_gates", "interval_seconds BETWEEN 1 AND 3600"
    )
    op.create_check_constraint("ck_meme_gate_version", "meme_send_gates", "row_version > 0")


def downgrade() -> None:
    raise RuntimeError("Keep saved meme intervals and send gates; use a compatible forward release")
