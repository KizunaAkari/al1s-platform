"""Add compatible transient media replies and native exit observation watermarks."""

from alembic import op
import sqlalchemy as sa

revision = "20261029_0062"
down_revision = "20261028_0061"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("notices_since", "exit_scan_at"):
        op.add_column(
            "freqtrade_futures_profiles",
            sa.Column(
                name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
        )
    for column in (
        sa.Column("image", sa.LargeBinary(), nullable=True),
        sa.Column("mention_id", sa.String(20), nullable=True),
        sa.Column("quote_message", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("quant_replies", column)
    op.create_check_constraint(
        "ck_quant_reply_image", "quant_replies", "image IS NULL OR octet_length(image) <= 524288"
    )
    op.create_index(
        "ix_quant_reply_expiry",
        "quant_replies",
        ["expires_at", "id"],
        postgresql_where=sa.text("expires_at IS NOT NULL AND status = 'queued'"),
    )


def downgrade():
    raise RuntimeError("visual_reply_downgrade_requires_explicit_queue_migration")
