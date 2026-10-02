"""Persist immutable lexicon sources and one atomic active pointer."""
import sqlalchemy as sa
from alembic import op

revision = "20260914_0028"
down_revision = "20260914_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "information_lexicon_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("commit", sa.String(40), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("normalization_version", sa.String(100), nullable=False),
        sa.Column("license_text", sa.Text(), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_information_lexicon_active", "information_lexicon_versions",
                    ["active"], unique=True, postgresql_where=sa.text("active"))
    op.create_index("uq_information_lexicon_version", "information_lexicon_versions",
                    ["commit", "normalization_version"], unique=True)


def downgrade() -> None:
    op.drop_table("information_lexicon_versions")
