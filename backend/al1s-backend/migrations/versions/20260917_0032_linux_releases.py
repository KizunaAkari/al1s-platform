"""Linux release catalog; additive to the 0029 -> 0030 -> 0031 release unit."""

import sqlalchemy as sa
from alembic import op

revision = "20260917_0032"
down_revision = "20260916_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "linux_releases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.String(64), nullable=False, unique=True),
        sa.Column("architecture", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("candidate_image", sa.String(160), nullable=False),
        sa.Column("expected_image_id", sa.String(71), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("blob_id", sa.Uuid(), sa.ForeignKey("blob_objects.id", ondelete="RESTRICT")),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "state IN ('draft','queued','verifying','published','failed')",
            name="ck_linux_release_state",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 5368709120", name="ck_linux_release_size"
        ),
        sa.CheckConstraint("sha256 ~ '^[a-f0-9]{64}$'", name="ck_linux_release_hash"),
        sa.CheckConstraint("architecture = 'arm64'", name="ck_linux_release_arch"),
        sa.CheckConstraint("row_version > 0", name="ck_linux_release_version"),
        sa.CheckConstraint(
            "state <> 'published' OR (blob_id IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_linux_release_publication",
        ),
    )
    op.create_index("ix_linux_release_created", "linux_releases", ["created_at", "id"])
    op.create_index("ix_linux_release_queue", "linux_releases", ["state", "lease_until", "id"])
    op.create_index("ix_linux_release_blob", "linux_releases", ["blob_id"])
    op.execute(
        "CREATE TRIGGER check_blob_reference BEFORE INSERT OR UPDATE OF blob_id "
        "ON linux_releases FOR EACH ROW EXECUTE FUNCTION al1s_check_blob_reference()"
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM linux_releases)")).scalar():
        raise RuntimeError("Cannot discard Linux release records")
    op.drop_table("linux_releases")
