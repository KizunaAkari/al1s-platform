"""Independent Bot targets and frozen complete QQ candidate sources."""

import sqlalchemy as sa
from alembic import op

revision = "20261013_0057"
down_revision = "20261009_0056"
branch_labels = None
depends_on = None

UNITS = "component IN ('platform','postgresql','s3','mqtt','linux-terminal','qq-bot','discord-bot')"
TARGET = (
    "(scope='platform' AND terminal_id IS NULL AND "
    "component NOT IN ('linux-terminal','qq-bot','discord-bot')) OR "
    "(scope='terminal:' || terminal_id::text AND terminal_id IS NOT NULL "
    "AND component='linux-terminal') OR "
    "(scope='bot:qq' AND terminal_id IS NULL AND component='qq-bot') OR "
    "(scope='bot:discord' AND terminal_id IS NULL AND component='discord-bot')"
)
SOURCE = (
    "(source_kind='archive' AND size_bytes IS NOT NULL AND sha256 IS NOT NULL "
    "AND candidate_image IS NOT NULL AND expected_image_id IS NOT NULL) OR "
    "(source_kind='registry' AND component='qq-bot' AND size_bytes IS NULL "
    "AND sha256 IS NULL AND blob_id IS NULL AND registry_digest IS NOT NULL "
    "AND platform_manifest_digest IS NOT NULL AND runtime_artifact_digest IS NOT NULL "
    "AND runtime_version IS NOT NULL AND registry_digest ~ '^sha256:[a-f0-9]{64}$' "
    "AND platform_manifest_digest ~ '^sha256:[a-f0-9]{64}$' "
    "AND runtime_artifact_digest ~ '^sha256:[a-f0-9]{64}$')"
)


def upgrade():
    for name, length in (
        ("source_kind", 16),
        ("registry_digest", 71),
        ("platform_manifest_digest", 71),
        ("runtime_artifact_digest", 71),
        ("runtime_version", 64),
        ("source_url", 256),
        ("source_notes", 4000),
    ):
        op.add_column(
            "component_releases",
            sa.Column(
                name,
                sa.String(length),
                nullable=name != "source_kind",
                server_default="archive" if name == "source_kind" else None,
            ),
        )
    op.add_column(
        "component_releases",
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name, typ in (
        ("size_bytes", sa.BigInteger()),
        ("sha256", sa.String(64)),
        ("candidate_image", sa.String(256)),
        ("expected_image_id", sa.String(71)),
    ):
        op.alter_column("component_releases", name, existing_type=typ, nullable=True)
    changes = {
        "ck_component_release_unit": UNITS,
        "ck_component_release_size": (
            "source_kind <> 'archive' OR (size_bytes > 0 AND size_bytes <= 5368709120)"
        ),
        "ck_component_release_hash": "source_kind <> 'archive' OR sha256 ~ '^[a-f0-9]{64}$'",
        "ck_component_release_published": (
            "state <> 'published' OR (published_at IS NOT NULL AND candidate_image IS NOT NULL "
            "AND expected_image_id IS NOT NULL AND (source_kind='registry' OR blob_id IS NOT NULL))"
        ),
    }
    for name, rule in changes.items():
        op.drop_constraint(name, "component_releases", type_="check")
        op.create_check_constraint(name, "component_releases", rule)
    op.create_check_constraint(
        "ck_component_release_source", "component_releases", "source_kind IN ('archive','registry')"
    )
    op.create_check_constraint("ck_component_release_source_identity", "component_releases", SOURCE)
    for name, rule in (
        ("ck_component_operation_unit", UNITS),
        ("ck_component_operation_target", TARGET),
    ):
        op.drop_constraint(name, "component_operations", type_="check")
        op.create_check_constraint(name, "component_operations", rule)


def downgrade():
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM component_releases WHERE source_kind='registry' "
            "OR component IN ('qq-bot','discord-bot')) OR EXISTS(SELECT 1 FROM "
            "component_operations WHERE component IN ('qq-bot','discord-bot'))"
        )
    ):
        raise RuntimeError("bot_update_data_prevents_downgrade")
    old = "component IN ('platform','postgresql','s3','mqtt','linux-terminal')"
    for table, name, rule in (
        ("component_releases", "ck_component_release_unit", old),
        (
            "component_releases",
            "ck_component_release_size",
            "size_bytes > 0 AND size_bytes <= 5368709120",
        ),
        ("component_releases", "ck_component_release_hash", "sha256 ~ '^[a-f0-9]{64}$'"),
        (
            "component_releases",
            "ck_component_release_published",
            "state <> 'published' OR (blob_id IS NOT NULL AND published_at IS NOT NULL)",
        ),
        ("component_operations", "ck_component_operation_unit", old),
        (
            "component_operations",
            "ck_component_operation_target",
            "(scope='platform' AND terminal_id IS NULL AND component <> 'linux-terminal') OR "
            "(scope='terminal:' || terminal_id::text AND terminal_id IS NOT NULL "
            "AND component='linux-terminal')",
        ),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, rule)
    for name in ("ck_component_release_source", "ck_component_release_source_identity"):
        op.drop_constraint(name, "component_releases", type_="check")
    for name in ("size_bytes", "sha256", "candidate_image", "expected_image_id"):
        op.alter_column("component_releases", name, nullable=False)
    for name in (
        "source_kind",
        "registry_digest",
        "platform_manifest_digest",
        "runtime_artifact_digest",
        "runtime_version",
        "source_url",
        "source_notes",
        "source_published_at",
    ):
        op.drop_column("component_releases", name)
