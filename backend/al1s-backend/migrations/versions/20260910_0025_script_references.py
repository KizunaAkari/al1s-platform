"""Index recovery dependencies and fence effective references against deletion.

Triggers cover candidate reuse/import as well as normal publication. Cross-row
FOR SHARE locks are intentional: foreign-key KEY SHARE does not fence soft delete.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0025"
down_revision = "20260909_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maa_script_references",
        sa.Column(
            "source_version_id",
            sa.Uuid(),
            sa.ForeignKey("maa_script_versions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("step_index", sa.Integer(), primary_key=True),
        sa.Column(
            "target_script_id",
            sa.Uuid(),
            sa.ForeignKey("maa_scripts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint("step_index > 0", name="ck_maa_script_references_step"),
    )
    op.create_index(
        "ix_maa_script_references_target",
        "maa_script_references",
        ["target_script_id", "source_version_id", "step_index"],
    )
    # A one-time database-side projection; never deserialize all history in the API.
    op.execute("""
        INSERT INTO maa_script_references (source_version_id, step_index, target_script_id)
        SELECT v.id, s.ordinality, (s.step->'failure_retry'->>'process_script_id')::uuid
        FROM maa_script_versions v,
             jsonb_array_elements(v.manifest->'steps') WITH ORDINALITY s(step, ordinality)
        WHERE s.step->'failure_retry'->'enabled' = 'true'::jsonb
    """)
    op.execute("""
        CREATE FUNCTION al1s_index_script_references() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO maa_script_references (source_version_id, step_index, target_script_id)
            SELECT NEW.id, s.ordinality, (s.step->'failure_retry'->>'process_script_id')::uuid
            FROM jsonb_array_elements(NEW.manifest->'steps') WITH ORDINALITY s(step, ordinality)
            WHERE s.step->'failure_retry'->'enabled' = 'true'::jsonb;
            RETURN NEW;
        END $$;
        CREATE TRIGGER index_script_references AFTER INSERT ON maa_script_versions
            FOR EACH ROW EXECUTE FUNCTION al1s_index_script_references();
    """)
    op.execute("""
        CREATE FUNCTION al1s_check_effective_script_references() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE target record;
        BEGIN
            IF NEW.deleted_at IS NOT NULL THEN RETURN NEW; END IF;
            FOR target IN
                SELECT t.id, t.deleted_at FROM maa_scripts t
                WHERE t.id IN (SELECT r.target_script_id FROM maa_script_references r
                    WHERE r.source_version_id IN (NEW.current_version_id, NEW.candidate_version_id))
                ORDER BY t.id FOR SHARE
            LOOP
                IF target.deleted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Recovery target was deleted'
                        USING ERRCODE='23514', CONSTRAINT='ck_maa_reference_active';
                END IF;
            END LOOP;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_effective_script_references
            BEFORE UPDATE OF current_version_id, candidate_version_id
            ON maa_scripts FOR EACH ROW EXECUTE FUNCTION al1s_check_effective_script_references();
        CREATE FUNCTION al1s_check_strategy_script_references() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE target record;
        BEGIN
            IF NEW.deleted_at IS NOT NULL THEN RETURN NEW; END IF;
            FOR target IN
                SELECT s.id, s.deleted_at FROM maa_scripts s WHERE s.id IN (
                    SELECT v.script_id FROM maa_script_versions v
                    JOIN maa_strategy_modules m ON m.script_version_id=v.id
                    WHERE m.strategy_version_id=NEW.current_version_id
                    UNION
                    SELECT r.target_script_id FROM maa_script_references r
                    JOIN maa_strategy_modules m ON m.script_version_id=r.source_version_id
                    WHERE m.strategy_version_id=NEW.current_version_id
                ) ORDER BY s.id FOR SHARE
            LOOP
                IF target.deleted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Strategy script was deleted'
                        USING ERRCODE='23514', CONSTRAINT='ck_maa_reference_active';
                END IF;
            END LOOP;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_strategy_script_references BEFORE UPDATE OF current_version_id
            ON maa_strategies FOR EACH ROW EXECUTE FUNCTION al1s_check_strategy_script_references();
    """)
    op.execute("""
        CREATE FUNCTION al1s_check_task_script_reference() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE removed_at timestamptz;
        BEGIN
            IF NEW.task_type = 'single' OR NEW.source_module <> 'maa'
                OR NEW.logical_content_id NOT LIKE 'script:%'
                THEN RETURN NEW; END IF;
            SELECT deleted_at INTO removed_at FROM maa_scripts
                WHERE id=substring(NEW.logical_content_id FROM 8)::uuid FOR SHARE;
            IF NOT FOUND OR removed_at IS NOT NULL THEN
                RAISE EXCEPTION 'Task script was deleted'
                    USING ERRCODE='23514', CONSTRAINT='ck_maa_reference_active';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_task_script_reference BEFORE INSERT ON task_requests
            FOR EACH ROW EXECUTE FUNCTION al1s_check_task_script_reference();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER check_task_script_reference ON task_requests")
    op.execute("DROP FUNCTION al1s_check_task_script_reference()")
    op.execute("DROP TRIGGER check_strategy_script_references ON maa_strategies")
    op.execute("DROP FUNCTION al1s_check_strategy_script_references()")
    op.execute("DROP TRIGGER check_effective_script_references ON maa_scripts")
    op.execute("DROP FUNCTION al1s_check_effective_script_references()")
    op.execute("DROP TRIGGER index_script_references ON maa_script_versions")
    op.execute("DROP FUNCTION al1s_index_script_references()")
    op.drop_table("maa_script_references")
