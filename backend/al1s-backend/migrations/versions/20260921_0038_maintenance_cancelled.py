"""Allow a host-confirmed cancellation before maintenance execution."""

from alembic import op

revision = "20260921_0038"
down_revision = "20260920_0037"
branch_labels = None
depends_on = None

STATES = (
    "'pending','accepted','executing','recovering','succeeded',"
    "'refused','expired','response_timeout','recovery_timeout','failed'"
)


def upgrade():
    op.drop_constraint("ck_maa_maintenance_state", "maa_maintenance_commands", type_="check")
    op.create_check_constraint(
        "ck_maa_maintenance_state", "maa_maintenance_commands",
        f"state IN ({STATES},'cancelled')",
    )


def downgrade():
    # Do not silently rewrite confirmed cancellation facts to failure on downgrade.
    op.drop_constraint("ck_maa_maintenance_state", "maa_maintenance_commands", type_="check")
    op.create_check_constraint(
        "ck_maa_maintenance_state", "maa_maintenance_commands", f"state IN ({STATES})",
    )
