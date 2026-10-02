"""Persist quick-test stop requests and bounded step events."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0043"
down_revision = "20260925_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("maa_quick_test_sessions", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column(
        "maa_quick_test_sessions", sa.Column("cancel_requested_at", sa.DateTime(timezone=True))
    )
    op.drop_constraint("ck_maa_quick_test_status", "maa_quick_test_sessions", type_="check")
    op.drop_constraint("ck_maa_quick_test_completion", "maa_quick_test_sessions", type_="check")
    op.create_check_constraint(
        "ck_maa_quick_test_status", "maa_quick_test_sessions",
        "status IN ('issued', 'claimed', 'completed', 'expired', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_maa_quick_test_completion", "maa_quick_test_sessions",
        "(status = 'issued' AND claimed_at IS NULL AND completed_at IS NULL "
        "AND qualification_receipt_id IS NULL AND started_at IS NULL) OR "
        "(status = 'claimed' AND claimed_at IS NOT NULL AND completed_at IS NULL "
        "AND qualification_receipt_id IS NULL) OR "
        "(status = 'completed' AND claimed_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NOT NULL) OR "
        "(status = 'expired' AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NULL) OR "
        "(status = 'cancelled' AND claimed_at IS NULL AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NULL)",
    )
    op.create_table(
        "maa_quick_test_events",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("step_number", sa.Integer()),
        sa.Column("code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["maa_quick_test_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("session_id", "sequence"),
        sa.CheckConstraint("sequence BETWEEN 1 AND 1000", name="ck_quick_event_sequence"),
        sa.CheckConstraint(
            "kind IN ('started','step_started','step_succeeded','step_failed','log')",
            name="ck_quick_event_kind",
        ),
        sa.CheckConstraint(
            "step_number IS NULL OR step_number BETWEEN 1 AND 1000",
            name="ck_quick_event_step",
        ),
    )
    op.create_index("ix_quick_event_retention", "maa_quick_test_events", ["created_at"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM maa_quick_test_events)")):
        raise RuntimeError("0043 downgrade would erase quick-test events")
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM maa_quick_test_sessions WHERE status = 'cancelled')"
    )):
        raise RuntimeError("0043 downgrade cannot represent cancelled quick tests")
    op.drop_index("ix_quick_event_retention", table_name="maa_quick_test_events")
    op.drop_table("maa_quick_test_events")
    op.drop_constraint("ck_maa_quick_test_completion", "maa_quick_test_sessions", type_="check")
    op.drop_constraint("ck_maa_quick_test_status", "maa_quick_test_sessions", type_="check")
    op.create_check_constraint(
        "ck_maa_quick_test_status", "maa_quick_test_sessions",
        "status IN ('issued', 'claimed', 'completed', 'expired')",
    )
    op.create_check_constraint(
        "ck_maa_quick_test_completion", "maa_quick_test_sessions",
        "(status = 'issued' AND claimed_at IS NULL AND completed_at IS NULL "
        "AND qualification_receipt_id IS NULL) OR "
        "(status = 'claimed' AND claimed_at IS NOT NULL AND completed_at IS NULL "
        "AND qualification_receipt_id IS NULL) OR "
        "(status = 'completed' AND claimed_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NOT NULL) OR "
        "(status = 'expired' AND completed_at IS NOT NULL "
        "AND qualification_receipt_id IS NULL)",
    )
    op.drop_column("maa_quick_test_sessions", "cancel_requested_at")
    op.drop_column("maa_quick_test_sessions", "started_at")
