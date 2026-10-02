"""Bind maintenance upgrades to immutable published release identities."""

import sqlalchemy as sa
from alembic import op

revision = "20260917_0033"
down_revision = "20260917_0032"
branch_labels = None
depends_on = None


def constraints(upgrade: bool) -> None:
    op.drop_constraint("ck_maa_maintenance_action", "maa_maintenance_commands", type_="check")
    op.drop_constraint("ck_maa_maintenance_state", "maa_maintenance_commands", type_="check")
    actions = "'restart_container','restart_host'" + (",'upgrade_container'" if upgrade else "")
    states = (
        "'pending','accepted','executing','recovering','succeeded','refused',"
        "'expired','response_timeout','recovery_timeout'" + (",'failed'" if upgrade else "")
    )
    op.create_check_constraint(
        "ck_maa_maintenance_action", "maa_maintenance_commands", f"action IN ({actions})"
    )
    op.create_check_constraint(
        "ck_maa_maintenance_state", "maa_maintenance_commands", f"state IN ({states})"
    )


def upgrade() -> None:
    op.add_column(
        "maa_maintenance_commands",
        sa.Column("release_id", sa.Uuid(), sa.ForeignKey("linux_releases.id", ondelete="RESTRICT")),
    )
    constraints(True)
    op.create_check_constraint(
        "ck_maa_maintenance_release",
        "maa_maintenance_commands",
        "(action = 'upgrade_container') = (release_id IS NOT NULL)",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM maa_maintenance_commands WHERE release_id IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Cannot discard upgrade command identities")
    op.drop_constraint("ck_maa_maintenance_release", "maa_maintenance_commands", type_="check")
    constraints(False)
    op.drop_column("maa_maintenance_commands", "release_id")
