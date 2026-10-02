"""Bounded technical-detail redaction; preserve event identities and audit summary."""
import sqlalchemy as sa
from alembic import op

revision = "20260914_0027"
down_revision = "20260914_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("outbox_events", "audit_logs"):
        op.add_column(name, sa.Column("details_purged_at", sa.DateTime(timezone=True)))
    op.create_index("ix_outbox_events_retention", "outbox_events",
                    ["owner_module", "published_at", "id"],
                    postgresql_where=sa.text("status = 'published' AND details_purged_at IS NULL"))
    op.create_index("ix_audit_logs_retention", "audit_logs",
                    ["owner_module", "created_at", "id"],
                    postgresql_where=sa.text("details_purged_at IS NULL"))


def downgrade() -> None:
    # Redacted content cannot be restored by a schema downgrade.
    for name in ("outbox_events", "audit_logs"):
        op.drop_index(f"ix_{name}_retention", table_name=name)
        op.drop_column(name, "details_purged_at")
