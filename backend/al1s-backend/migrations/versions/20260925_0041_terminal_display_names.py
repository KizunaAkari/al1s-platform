"""Keep administrator terminal names across registration and heartbeats."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0041"
down_revision = "20260924_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "terminals",
        sa.Column("name_is_custom", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "terminals",
        sa.Column("name_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint("ck_terminals_name_version", "terminals", "name_version >= 0")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM terminals WHERE name_is_custom) "
        "THEN RAISE EXCEPTION 'administrator terminal names would lose protection'; "
        "END IF; END $$"
    )
    op.drop_constraint("ck_terminals_name_version", "terminals", type_="check")
    op.drop_column("terminals", "name_version")
    op.drop_column("terminals", "name_is_custom")
