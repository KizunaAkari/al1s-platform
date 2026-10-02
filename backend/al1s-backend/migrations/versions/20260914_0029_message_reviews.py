"""Encrypted review body plus retained source tombstone, without raw outbox copies."""

import sqlalchemy as sa
from alembic import op

revision = "20260914_0029"
down_revision = "20260914_0028"
branch_labels = None
depends_on = None

KINDS = "'conditional_skip', 'script_failure', 'storage_low', 'test'"


def upgrade() -> None:
    op.create_table(
        "information_message_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("bot_services.id"), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("body_cipher", sa.Text()),
        sa.Column("key_id", sa.String(64)),
        sa.Column("lexicon_commit", sa.String(40)),
        sa.Column("normalization_version", sa.String(100)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending_at", sa.DateTime(timezone=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), sa.ForeignKey("notification_intents.id"), unique=True),
        sa.CheckConstraint(
            "state IN ('hold','pending','approved','rejected','expired','completed')",
            name="ck_information_review_state",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_information_review_version"),
        sa.CheckConstraint(
            "(body_cipher IS NULL) = (key_id IS NULL)", name="ck_information_review_cipher"
        ),
    )
    op.create_index(
        "uq_information_review_source",
        "information_message_reviews",
        ["service_id", "message_id"],
        unique=True,
    )
    op.create_index(
        "ix_information_review_state", "information_message_reviews", ["state", "received_at", "id"]
    )
    op.create_index(
        "ix_information_review_finalized", "information_message_reviews", ["finalized_at", "id"]
    )
    op.create_index(
        "ix_information_review_checked", "information_message_reviews", ["checked_at", "id"]
    )
    for table in ("notification_routes", "notification_intents"):
        op.drop_constraint(f"ck_{table}_kind", table, type_="check")
        op.create_check_constraint(
            f"ck_{table}_kind", table, f"notification_kind IN ({KINDS}, 'forward')"
        )


def downgrade() -> None:
    # Downgrade intentionally fails while forward records remain; do not erase valid data.
    for table in ("notification_routes", "notification_intents"):
        op.drop_constraint(f"ck_{table}_kind", table, type_="check")
        op.create_check_constraint(f"ck_{table}_kind", table, f"notification_kind IN ({KINDS})")
    op.drop_table("information_message_reviews")
