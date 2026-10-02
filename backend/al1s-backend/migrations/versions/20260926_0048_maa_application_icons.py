"""Store bounded category icons with their application records."""

import sqlalchemy as sa
from alembic import op

revision = "20260926_0048"
down_revision = "20260926_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("maa_applications", sa.Column("icon_png", sa.LargeBinary(), nullable=True))
    op.create_check_constraint(
        "ck_maa_app_icon_size", "maa_applications",
        "icon_png IS NULL OR octet_length(icon_png) <= 65536",
    )


def downgrade() -> None:
    op.drop_constraint("ck_maa_app_icon_size", "maa_applications", type_="check")
    op.drop_column("maa_applications", "icon_png")
