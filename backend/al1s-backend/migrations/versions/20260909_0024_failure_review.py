"""Failure review receipts and concurrency-safe blob reference fencing."""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0024"
down_revision = "20260909_0023"
branch_labels = None
depends_on = None

TABLES = (
    "terminal_artifacts", "maa_script_version_blobs", "maa_quick_test_blobs",
    "execution_snapshot_blobs",
)


def upgrade() -> None:
    op.add_column("terminal_reports", sa.Column("detail_confirmed_at", sa.DateTime(timezone=True)))
    op.add_column("terminal_artifacts", sa.Column("downloaded_at", sa.DateTime(timezone=True)))
    # FOR SHARE conflicts with GC's row lock, unlike the FK's KEY SHARE lock.
    op.execute("""
        CREATE FUNCTION al1s_check_blob_reference() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_status text;
        BEGIN
            IF NEW.blob_id IS NULL THEN RETURN NEW; END IF;
            SELECT status INTO current_status FROM blob_objects
                WHERE id = NEW.blob_id FOR SHARE;
            IF current_status IS NULL OR current_status NOT IN ('pending', 'ready') THEN
                RAISE EXCEPTION 'Blob is not referenceable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in TABLES:
        op.execute(f"CREATE TRIGGER check_blob_reference BEFORE INSERT OR UPDATE OF blob_id "
                   f"ON {table} FOR EACH ROW EXECUTE FUNCTION al1s_check_blob_reference()")


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM terminal_reports WHERE detail_confirmed_at IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM terminal_artifacts WHERE downloaded_at IS NOT NULL)"
    )).scalar():
        raise RuntimeError("Cannot discard failure confirmation receipts")
    for table in TABLES:
        op.execute(f"DROP TRIGGER check_blob_reference ON {table}")
    op.execute("DROP FUNCTION al1s_check_blob_reference()")
    op.drop_column("terminal_artifacts", "downloaded_at")
    op.drop_column("terminal_reports", "detail_confirmed_at")
