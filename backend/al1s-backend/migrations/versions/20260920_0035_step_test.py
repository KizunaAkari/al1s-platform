"""Keep single-step results separate from full-script publication qualification."""

from alembic import op

revision = "20260920_0035"
down_revision = "20260917_0034"
branch_labels = None
depends_on = None
TABLE = "maa_script_qualification_receipts"


def _constraints(kinds: str) -> None:
    op.create_check_constraint(
        "ck_maa_qualification_kind", TABLE, f"kind IN ('static_check', {kinds})"
    )
    op.create_check_constraint(
        "ck_maa_qualification_execution_context",
        TABLE,
        "(kind = 'static_check' AND terminal_id IS NULL AND target_device_id IS NULL) OR "
        f"(kind IN ({kinds}) AND terminal_id IS NOT NULL AND target_device_id IS NOT NULL)",
    )


def upgrade() -> None:
    op.drop_constraint("ck_maa_qualification_kind", TABLE, type_="check")
    op.drop_constraint("ck_maa_qualification_execution_context", TABLE, type_="check")
    _constraints("'quick_test', 'step_test'")


def downgrade() -> None:
    # Existing step-test receipts prevent downgrade; never delete user results.
    op.drop_constraint("ck_maa_qualification_kind", TABLE, type_="check")
    op.drop_constraint("ck_maa_qualification_execution_context", TABLE, type_="check")
    _constraints("'quick_test'")
