"""Typed component releases and upgrade records; existing script tables are untouched."""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0052"
down_revision = "20261001_0051"
branch_labels = None
depends_on = None


def release_table() -> None:
    op.create_table(
        "component_releases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("architecture", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("candidate_image", sa.String(256), nullable=False),
        sa.Column("expected_image_id", sa.String(71), nullable=False),
        sa.Column("native_version", sa.String(64), nullable=False),
        sa.Column("compatible_from", sa.String(128), nullable=False),
        sa.Column("data_policy", sa.String(32), nullable=False),
        sa.Column("schema_from", sa.String(64)),
        sa.Column("schema_to", sa.String(64)),
        sa.Column("maa_version", sa.String(64)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("blob_id", sa.Uuid(), sa.ForeignKey("blob_objects.id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "component", "version", "architecture", name="uq_component_release_version"
        ),
        sa.CheckConstraint(
            "component IN ('platform','postgresql','s3','mqtt','linux-terminal')",
            name="ck_component_release_unit",
        ),
        sa.CheckConstraint(
            "state IN ('draft','queued','verifying','published','failed')",
            name="ck_component_release_state",
        ),
        sa.CheckConstraint("architecture IN ('amd64','arm64')", name="ck_component_release_arch"),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 5368709120", name="ck_component_release_size"
        ),
        sa.CheckConstraint("sha256 ~ '^[a-f0-9]{64}$'", name="ck_component_release_hash"),
        sa.CheckConstraint("row_version > 0", name="ck_component_release_row_version"),
        sa.CheckConstraint(
            "state <> 'published' OR (blob_id IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_component_release_published",
        ),
    )
    for name, columns in [
        ("ix_component_release_page", ["component", "created_at", "id"]),
        ("ix_component_release_queue", ["state", "lease_until", "id"]),
        ("ix_component_release_blob", ["blob_id"]),
    ]:
        op.create_index(name, "component_releases", columns)
    op.execute(
        "CREATE TRIGGER check_blob_reference BEFORE INSERT OR UPDATE OF blob_id "
        "ON component_releases FOR EACH ROW EXECUTE FUNCTION al1s_check_blob_reference()"
    )


def operation_table() -> None:
    op.create_table(
        "component_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), sa.ForeignKey("terminals.id", ondelete="RESTRICT")),
        sa.Column(
            "release_id",
            sa.Uuid(),
            sa.ForeignKey("component_releases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("expected_source", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("recovery_point", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_component_operation_key"),
        sa.CheckConstraint(
            "state IN ('accepted','preparing','switching','verifying',"
            "'succeeded','failed_safe','unknown')",
            name="ck_component_operation_state",
        ),
        sa.CheckConstraint(
            "(scope = 'platform' AND terminal_id IS NULL AND component <> 'linux-terminal') OR "
            "(scope = 'terminal:' || terminal_id::text AND terminal_id IS NOT NULL "
            "AND component = 'linux-terminal')",
            name="ck_component_operation_target",
        ),
        sa.CheckConstraint(
            "component IN ('platform','postgresql','s3','mqtt','linux-terminal')",
            name="ck_component_operation_unit",
        ),
        sa.CheckConstraint(
            "expected_source ~ '^[a-f0-9]{64}$'", name="ck_component_operation_source"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_component_operation_version"),
    )
    op.create_index("ix_component_operation_page", "component_operations", ["created_at", "id"])
    op.create_index(
        "uq_component_operation_active_scope",
        "component_operations",
        ["scope"],
        unique=True,
        postgresql_where=sa.text("state NOT IN ('succeeded','failed_safe')"),
    )


def upgrade() -> None:
    release_table()
    operation_table()
    admission_triggers()


def admission_triggers() -> None:
    # Enforce the same admission fence for SQL/bulk writers, not only API callers.
    op.execute("""
        CREATE FUNCTION al1s_component_admission() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target uuid;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended('al1s-component-admission',0));
            IF TG_TABLE_NAME = 'task_requests' THEN
                target := NEW.requested_terminal_id;
                IF target IS NULL AND NEW.requested_target_device_id IS NOT NULL THEN
                    SELECT managing_terminal_id INTO target FROM target_devices
                        WHERE id = NEW.requested_target_device_id;
                END IF;
            ELSE
                target := NEW.terminal_id;
            END IF;
            IF EXISTS (SELECT 1 FROM component_operations
                WHERE state IN ('accepted','preparing','switching','verifying','unknown')
                  AND (scope = 'platform' OR scope = 'terminal:' || target::text)) THEN
                RAISE EXCEPTION 'Component maintenance blocks new work'
                    USING ERRCODE = '23514', CONSTRAINT = 'component_admission';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("task_requests", "executions", "maa_quick_test_sessions"):
        op.execute(f"CREATE TRIGGER aa_component_admission BEFORE INSERT ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION al1s_component_admission()")


def downgrade() -> None:
    # Published versions or operations carry retained recovery/Blob facts, never discard them.
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM component_operations) OR "
            "EXISTS(SELECT 1 FROM component_releases WHERE state = 'published')"
        )
    ):
        raise RuntimeError("Component records must be retained")
    for table in ("task_requests", "executions", "maa_quick_test_sessions"):
        op.execute(f"DROP TRIGGER aa_component_admission ON {table}")
    op.execute("DROP FUNCTION al1s_component_admission()")
    op.drop_table("component_operations")
    op.drop_table("component_releases")
