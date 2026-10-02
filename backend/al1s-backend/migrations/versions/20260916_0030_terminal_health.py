"""Terminal alert routes and bounded heartbeat expiry lookup."""
import sqlalchemy as sa
from alembic import op

revision = "20260916_0030"
down_revision = "20260914_0029"
branch_labels = None
depends_on = None

KINDS = "'conditional_skip', 'script_failure', 'storage_low', 'test', 'forward'"


def upgrade() -> None:
    for table in ("notification_routes", "notification_intents"):
        op.drop_constraint(f"ck_{table}_kind", table, type_="check")
        op.create_check_constraint(f"ck_{table}_kind", table,
                                   f"notification_kind IN ({KINDS}, 'terminal_alert')")
    op.create_index("ix_terminals_heartbeat_deadline", "terminals", ["last_seen_at", "id"],
                    postgresql_where=sa.text("deleted_at IS NULL AND service_status = 'online'"))


def downgrade() -> None:
    # Existing terminal alerts must not be silently deleted to permit a downgrade.
    for table in ("notification_routes", "notification_intents"):
        op.drop_constraint(f"ck_{table}_kind", table, type_="check")
        op.create_check_constraint(f"ck_{table}_kind", table, f"notification_kind IN ({KINDS})")
    op.drop_index("ix_terminals_heartbeat_deadline", table_name="terminals")
