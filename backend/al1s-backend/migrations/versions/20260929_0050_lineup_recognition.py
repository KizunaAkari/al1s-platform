"""Add persistent lineup image records and GC reference fencing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260929_0050"
down_revision = "20260927_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lineup_recognitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("blob_id", sa.Uuid(), sa.ForeignKey("blob_objects.id"), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), sa.ForeignKey("terminals.id")),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("task_requests.id"), unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review", JSONB(none_as_null=True)),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "width > 0 AND height > 0 AND row_version > 0", name="ck_lineup_dimensions"
        ),
    )
    op.create_index("ix_lineup_blob", "lineup_recognitions", ["blob_id"])
    op.create_index("ix_lineup_history", "lineup_recognitions", ["created_at", "id"])
    op.execute(
        "CREATE TRIGGER check_blob_reference BEFORE INSERT OR UPDATE OF blob_id "
        "ON lineup_recognitions FOR EACH ROW EXECUTE FUNCTION al1s_check_blob_reference()"
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM lineup_recognitions)")).scalar():
        raise RuntimeError("Cannot discard saved lineup recognition records")
    op.drop_table("lineup_recognitions")
