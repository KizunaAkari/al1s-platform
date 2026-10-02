"""Allow opt-in operation failure notification routes without reusing script failures."""
from alembic import op

revision = "20260921_0039"
down_revision = "20260921_0038"
branch_labels = None
depends_on = None

KINDS = "'conditional_skip', 'script_failure', 'storage_low', 'test', 'forward', 'terminal_alert'"


def upgrade() -> None:
    for table in ("notification_routes", "notification_intents"):
        op.drop_constraint(f"ck_{table}_kind", table, type_="check")
        op.create_check_constraint(f"ck_{table}_kind", table,
                                   f"notification_kind IN ({KINDS}, 'operation_failure')")


def downgrade() -> None:
    # Refuse incompatible rollback rather than deleting existing notification facts.
    for table in ("notification_routes", "notification_intents"):
        op.drop_constraint(f"ck_{table}_kind", table, type_="check")
        op.create_check_constraint(f"ck_{table}_kind", table,
                                   f"notification_kind IN ({KINDS})")
