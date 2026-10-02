"""Independent recipient deliveries. Old records must be archived before upgrade."""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0022"
down_revision = "20260908_0021"
branch_labels = None
depends_on = None


def _require_empty() -> None:
    # Serialize against a sender/materializer; never discard notification history here.
    op.execute("LOCK TABLE notification_deliveries IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM notification_deliveries)")):
        raise RuntimeError("Archive legacy notification deliveries to ZIP and clear them first")


def upgrade() -> None:
    _require_empty()
    op.drop_constraint("uq_notification_delivery_intent_route", "notification_deliveries")
    op.add_column(
        "notification_deliveries",
        sa.Column(
            "target_key",
            sa.Text(),
            sa.Computed("targets ->> 0", persisted=True),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_notification_single_target",
        "notification_deliveries",
        "jsonb_array_length(targets) = 1",
    )
    op.create_unique_constraint(
        "uq_notification_delivery_target",
        "notification_deliveries",
        ["intent_id", "route_id", "channel_id", "target_key"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    _require_empty()
    op.drop_constraint("uq_notification_delivery_target", "notification_deliveries")
    op.drop_constraint("ck_notification_single_target", "notification_deliveries")
    op.drop_column("notification_deliveries", "target_key")
    op.create_unique_constraint(
        "uq_notification_delivery_intent_route",
        "notification_deliveries",
        ["intent_id", "route_id"],
    )
