"""Batch lineup tasks and versioned original-image human annotations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261001_0051"
down_revision = "20260929_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_task_requests_type", "task_requests", type_="check")
    op.create_check_constraint(
        "ck_task_requests_type", "task_requests", "task_type IN ('single','loop','timed','batch')"
    )
    op.drop_constraint("lineup_recognitions_task_id_key", "lineup_recognitions", type_="unique")
    op.add_column(
        "lineup_recognitions",
        sa.Column("occurrence_id", sa.Uuid(), sa.ForeignKey("plan_occurrences.id"), unique=True),
    )
    op.add_column(
        "lineup_recognitions",
        sa.Column("retry_source_record_id", sa.Uuid(), sa.ForeignKey("lineup_recognitions.id")),
    )
    op.add_column(
        "lineup_recognitions",
        sa.Column("needs_attention", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "lineup_recognitions",
        sa.Column("attention_reason", sa.String(30), nullable=False, server_default="none"),
    )
    op.create_index(
        "ix_lineup_task_attention",
        "lineup_recognitions",
        ["task_id", "needs_attention", "occurrence_id"],
    )
    op.create_table(
        "lineup_batches",
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("task_requests.id"), primary_key=True),
        sa.Column("retry_source_task_id", sa.Uuid(), sa.ForeignKey("task_requests.id")),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(100), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.CheckConstraint("item_count BETWEEN 1 AND 200", name="ck_lineup_batch_size"),
    )
    op.create_index(
        "ix_lineup_batches_retry_source_task_id", "lineup_batches", ["retry_source_task_id"]
    )
    op.create_table(
        "lineup_submission_receipts",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("task_requests.id"), nullable=False),
    )
    op.create_index(
        "ix_lineup_submission_receipts_task_id", "lineup_submission_receipts", ["task_id"]
    )
    op.create_table(
        "lineup_annotations",
        sa.Column(
            "record_id", sa.Uuid(), sa.ForeignKey("lineup_recognitions.id"), primary_key=True
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attack_size", sa.Integer(), nullable=False),
        sa.Column("defense_size", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version > 0 AND state IN ('draft','confirmed')", name="ck_lineup_annotation"
        ),
        sa.CheckConstraint(
            "attack_size BETWEEN 0 AND 6 AND defense_size BETWEEN 0 AND 6",
            name="ck_lineup_annotation_sizes",
        ),
    )
    op.create_table(
        "lineup_annotation_slots",
        sa.Column(
            "record_id", sa.Uuid(), sa.ForeignKey("lineup_annotations.record_id"), primary_key=True
        ),
        sa.Column("side", sa.String(10), primary_key=True),
        sa.Column("slot_index", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer()),
        sa.CheckConstraint(
            "side IN ('attack','defense') AND slot_index BETWEEN 0 AND 5",
            name="ck_lineup_annotation_slot",
        ),
    )
    op.create_table(
        "lineup_annotation_regions",
        sa.Column("record_id", sa.Uuid(), primary_key=True),
        sa.Column("side", sa.String(10), primary_key=True),
        sa.Column("slot_index", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(10), primary_key=True),
        *(sa.Column(n, sa.Integer(), nullable=False) for n in ("x", "y", "width", "height")),
        sa.ForeignKeyConstraint(
            ["record_id", "side", "slot_index"],
            [
                "lineup_annotation_slots.record_id",
                "lineup_annotation_slots.side",
                "lineup_annotation_slots.slot_index",
            ],
        ),
        sa.CheckConstraint(
            "kind IN ('portrait','name') AND x >= 0 AND y >= 0 AND width > 0 AND height > 0",
            name="ck_lineup_annotation_region",
        ),
    )

    # Failure projection is inside the existing execution UPDATE transaction.
    # A trigger avoids an extra per-attempt DB round trip in the timeout worker.
    op.execute("""
        CREATE FUNCTION al1s_lineup_execution_failure() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.result = 'failure' OR NEW.status = 'timed_out' THEN
            UPDATE lineup_recognitions r
            SET needs_attention=true, attention_reason='runtime_failure'
            WHERE (r.occurrence_id=NEW.occurrence_id OR
                   (r.occurrence_id IS NULL AND r.task_id=NEW.task_request_id))
              AND NOT EXISTS (SELECT 1 FROM lineup_annotations a
                              WHERE a.record_id=r.id AND a.state='confirmed');
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER lineup_execution_failure AFTER UPDATE OF status,result ON executions
        FOR EACH ROW EXECUTE FUNCTION al1s_lineup_execution_failure();
    """)


def downgrade() -> None:
    used = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM lineup_batches) "
                "OR EXISTS(SELECT 1 FROM lineup_annotations)"
            )
        )
        .scalar()
    )
    if used:
        raise RuntimeError("Cannot discard saved lineup batches or human annotations")
    op.execute("DROP TRIGGER IF EXISTS lineup_execution_failure ON executions")
    op.execute("DROP FUNCTION IF EXISTS al1s_lineup_execution_failure()")
    for table in (
        "lineup_annotation_regions",
        "lineup_annotation_slots",
        "lineup_annotations",
        "lineup_submission_receipts",
        "lineup_batches",
    ):
        op.drop_table(table)
    op.drop_index("ix_lineup_task_attention", table_name="lineup_recognitions")
    for column in (
        "attention_reason",
        "needs_attention",
        "retry_source_record_id",
        "occurrence_id",
    ):
        op.drop_column("lineup_recognitions", column)
    op.create_unique_constraint(
        "lineup_recognitions_task_id_key", "lineup_recognitions", ["task_id"]
    )
    op.drop_constraint("ck_task_requests_type", "task_requests", type_="check")
    op.create_check_constraint(
        "ck_task_requests_type", "task_requests", "task_type IN ('single','loop','timed')"
    )
