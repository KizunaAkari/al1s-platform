"""Keep the accepted event sequence after seven-day detail cleanup."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0044"
down_revision = "20260925_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "maa_quick_test_sessions",
        sa.Column("event_last_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE maa_quick_test_sessions AS s SET event_last_sequence = "
        "(SELECT COALESCE(MAX(e.sequence), 0) FROM maa_quick_test_events AS e "
        "WHERE e.session_id = s.id)"
    )
    op.create_check_constraint(
        "ck_maa_quick_test_event_sequence", "maa_quick_test_sessions",
        "event_last_sequence BETWEEN 0 AND 1000",
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM maa_quick_test_sessions "
        "WHERE event_last_sequence > 0)"
    )):
        raise RuntimeError("0044 downgrade would erase accepted quick-test sequence state")
    op.drop_constraint(
        "ck_maa_quick_test_event_sequence", "maa_quick_test_sessions", type_="check"
    )
    op.drop_column("maa_quick_test_sessions", "event_last_sequence")
