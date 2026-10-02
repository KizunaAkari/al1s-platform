"""Persist the display order of saved Maa scripts."""

import sqlalchemy as sa
from alembic import op

revision = "20260926_0047"
down_revision = "20260925_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE maa_script_display_order_seq")
    op.add_column("maa_scripts", sa.Column("display_order", sa.Numeric(), nullable=True))
    op.execute(
        "UPDATE maa_scripts AS script SET display_order = ranked.position "
        "FROM (SELECT id, row_number() OVER (ORDER BY id) AS position FROM maa_scripts) AS ranked "
        "WHERE script.id = ranked.id"
    )
    op.execute(
        "SELECT setval('maa_script_display_order_seq', "
        "COALESCE((SELECT max(display_order)::bigint FROM maa_scripts), 1), "
        "EXISTS (SELECT 1 FROM maa_scripts))"
    )
    op.alter_column(
        "maa_scripts", "display_order", nullable=False,
        server_default=sa.text("nextval('maa_script_display_order_seq')"),
    )
    op.create_index(
        "ix_maa_scripts_display_order", "maa_scripts",
        ["application_id", "display_order", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_maa_scripts_display_order", table_name="maa_scripts")
    op.drop_column("maa_scripts", "display_order")
    op.execute("DROP SEQUENCE maa_script_display_order_seq")
