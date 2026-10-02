"""Attribute shared communication/audit rows without deleting existing facts."""
import sqlalchemy as sa
from alembic import op

revision = "20260914_0026"
down_revision = "20260910_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("outbox_events", "audit_logs"):
        op.add_column(name, sa.Column(
            "owner_module", sa.String(32), nullable=False,
            server_default=sa.text("'unassigned'"),
        ))
        # Historical records whose source is not demonstrable remain protected.
        source = "aggregate_type" if name == "outbox_events" else "target_type"
        rows = sa.table(name, sa.column("owner_module"), sa.column(source))
        field = rows.c[source]
        op.execute(rows.update().where(
            sa.or_(field.startswith("notification_", autoescape=True),
                   field.startswith("bot_", autoescape=True))
        ).values(owner_module="information"))
        op.execute(rows.update().where(
            sa.or_(field.startswith("maa_", autoescape=True),
                   field.in_(("execution", "terminal", "target_device", "task_schedule")))
        ).values(owner_module="maa"))
        op.create_check_constraint(
            f"ck_{name}_owner_module", name,
            "owner_module IN ('platform', 'maa', 'information', 'unassigned')",
        )
    op.create_index("ix_outbox_events_owner_dispatch", "outbox_events",
                    ["owner_module", "status", "available_at"])
    op.create_index("ix_audit_logs_owner_created", "audit_logs",
                    ["owner_module", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_owner_created", table_name="audit_logs")
    op.drop_index("ix_outbox_events_owner_dispatch", table_name="outbox_events")
    op.drop_constraint("ck_audit_logs_owner_module", "audit_logs", type_="check")
    op.drop_constraint("ck_outbox_events_owner_module", "outbox_events", type_="check")
    op.drop_column("audit_logs", "owner_module")
    op.drop_column("outbox_events", "owner_module")
