"""Shared meme templates, protected image references and original-message send receipts.

Frozen SQLAlchemy-generated PostgreSQL DDL; never imports mutable application mappings.
"""

import json
from uuid import UUID

from alembic import op
from sqlalchemy import text

revision = "20261008_0055"
down_revision = "20261006_0054"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE meme_templates ( id UUID NOT NULL, name VARCHAR(64) NOT NULL, engine_kind
        VARCHAR(16) NOT NULL, enabled BOOLEAN NOT NULL, random_enabled BOOLEAN NOT NULL,
        builtin BOOLEAN NOT NULL, current_version_id UUID, row_version INTEGER NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL, updated_at TIMESTAMP WITH TIME
        ZONE NOT NULL, deleted_at TIMESTAMP WITH TIME ZONE, PRIMARY KEY (id), CONSTRAINT
        ck_meme_engine CHECK (engine_kind IN ('petpet','caption','composite')),
        CONSTRAINT ck_meme_template_version CHECK (row_version > 0) )""",
    """CREATE INDEX ix_meme_random ON meme_templates (enabled, random_enabled, deleted_at)""",
    """CREATE INDEX ix_meme_template_page ON meme_templates (deleted_at, id)""",
    """CREATE TABLE meme_template_versions ( id UUID NOT NULL, template_id UUID NOT NULL,
        version_no INTEGER NOT NULL, schema_version INTEGER NOT NULL, min_avatar_count
        INTEGER NOT NULL, min_image_count INTEGER NOT NULL, min_text_count INTEGER NOT
        NULL, layout JSONB NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        lease_until TIMESTAMP WITH TIME ZONE, PRIMARY KEY (id), CONSTRAINT
        uq_meme_version_number UNIQUE (template_id, version_no), CONSTRAINT
        uq_meme_version_owner UNIQUE (template_id, id), CONSTRAINT
        ck_meme_schema_version CHECK (version_no > 0 AND schema_version = 1), CONSTRAINT
        ck_meme_inputs CHECK (min_avatar_count BETWEEN 0 AND 4 AND min_image_count
        BETWEEN 0 AND 1 AND min_text_count BETWEEN 0 AND 4), FOREIGN KEY(template_id)
        REFERENCES meme_templates (id) ON DELETE RESTRICT )""",
    """CREATE INDEX ix_meme_version_cleanup ON meme_template_versions (lease_until, created_at,
        id)""",
    """CREATE TABLE meme_template_aliases ( normalized_alias VARCHAR(32) NOT NULL, template_id
        UUID NOT NULL, is_primary BOOLEAN NOT NULL, PRIMARY KEY (normalized_alias),
        FOREIGN KEY(template_id) REFERENCES meme_templates (id) ON DELETE RESTRICT )""",
    """CREATE INDEX ix_meme_alias_template ON meme_template_aliases (template_id)""",
    """CREATE TABLE meme_assets ( blob_id UUID NOT NULL, width INTEGER NOT NULL, height INTEGER
        NOT NULL, expires_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (blob_id),
        CONSTRAINT ck_meme_asset_dimensions CHECK (width BETWEEN 1 AND 4096 AND height
        BETWEEN 1 AND 4096 AND width::bigint * height <= 8000000), FOREIGN KEY(blob_id)
        REFERENCES blob_objects (id) ON DELETE RESTRICT )""",
    """CREATE INDEX ix_meme_asset_cleanup ON meme_assets (expires_at, blob_id)""",
    """CREATE TABLE meme_template_version_blobs ( version_id UUID NOT NULL, role VARCHAR(16)
        NOT NULL, blob_id UUID NOT NULL, PRIMARY KEY (version_id, role), CONSTRAINT
        fk_meme_blob_owned FOREIGN KEY(blob_id) REFERENCES meme_assets (blob_id) ON
        DELETE RESTRICT, CONSTRAINT ck_meme_blob_role CHECK (role IN
        ('background','foreground')), FOREIGN KEY(version_id) REFERENCES
        meme_template_versions (id) ON DELETE CASCADE, FOREIGN KEY(blob_id) REFERENCES
        blob_objects (id) ON DELETE RESTRICT )""",
    """CREATE INDEX ix_meme_blob_reference ON meme_template_version_blobs (blob_id)""",
    """CREATE TABLE meme_bot_settings ( bot_service_id UUID NOT NULL, enabled BOOLEAN NOT NULL,
        private_enabled BOOLEAN NOT NULL, qq_event_ws_url VARCHAR(1024),
        cooldown_seconds INTEGER NOT NULL, row_version INTEGER NOT NULL, PRIMARY KEY
        (bot_service_id), CONSTRAINT ck_meme_cooldown CHECK (cooldown_seconds BETWEEN 1
        AND 60), CONSTRAINT ck_meme_settings_version CHECK (row_version > 0), FOREIGN
        KEY(bot_service_id) REFERENCES bot_services (id) ON DELETE RESTRICT )""",
    """CREATE TABLE meme_bot_scopes ( bot_service_id UUID NOT NULL, scope_kind VARCHAR(32) NOT
        NULL, target_id VARCHAR(20) NOT NULL, guild_id VARCHAR(20), PRIMARY KEY
        (bot_service_id, scope_kind, target_id), CONSTRAINT ck_meme_scope_kind CHECK
        (scope_kind IN ('qq_group','discord_channel')), FOREIGN KEY(bot_service_id)
        REFERENCES meme_bot_settings (bot_service_id) ON DELETE CASCADE )""",
    """CREATE TABLE meme_command_receipts ( id UUID NOT NULL, bot_service_id UUID NOT NULL,
        source_message_id VARCHAR(64) NOT NULL, scope_kind VARCHAR(32) NOT NULL,
        target_id VARCHAR(20) NOT NULL, sender_id VARCHAR(20) NOT NULL, guild_id
        VARCHAR(20), template_version_id UUID, status VARCHAR(16) NOT NULL, error_code
        VARCHAR(64), claimed_at TIMESTAMP WITH TIME ZONE NOT NULL, expires_at TIMESTAMP
        WITH TIME ZONE NOT NULL, PRIMARY KEY (id), CONSTRAINT uq_meme_source_message
        UNIQUE (bot_service_id, source_message_id), CONSTRAINT ck_meme_receipt_status
        CHECK (status IN ('claimed','send_started','sent','failed','unknown')),
        CONSTRAINT ck_meme_receipt_scope CHECK (scope_kind IN
        ('qq_group','qq_private','discord_channel','discord_private')), FOREIGN
        KEY(bot_service_id) REFERENCES bot_services (id) ON DELETE RESTRICT, FOREIGN
        KEY(template_version_id) REFERENCES meme_template_versions (id) ON DELETE
        RESTRICT )""",
    """CREATE INDEX ix_meme_receipt_cleanup ON meme_command_receipts (expires_at, id)""",
    """CREATE INDEX ix_meme_receipt_version ON meme_command_receipts (template_version_id)""",
    """CREATE TABLE meme_mutations ( key VARCHAR(64) NOT NULL, request_sha256 VARCHAR(64) NOT
        NULL, template_id UUID, bot_service_id UUID, version_id UUID, result JSONB NOT
        NULL, expires_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (key),
        CONSTRAINT ck_meme_mutation_owner CHECK ((template_id IS NOT NULL)::int +
        (bot_service_id IS NOT NULL)::int = 1), FOREIGN KEY(template_id) REFERENCES
        meme_templates (id) ON DELETE RESTRICT, FOREIGN KEY(bot_service_id) REFERENCES
        bot_services (id) ON DELETE RESTRICT, FOREIGN KEY(version_id) REFERENCES
        meme_template_versions (id) ON DELETE RESTRICT )""",
    """CREATE INDEX ix_meme_mutation_cleanup ON meme_mutations (expires_at, key)""",
    """CREATE INDEX ix_meme_mutation_version ON meme_mutations (version_id)""",
    """ALTER TABLE meme_templates ADD CONSTRAINT fk_meme_current_version FOREIGN KEY(id,
        current_version_id) REFERENCES meme_template_versions (template_id, id)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TRIGGER check_blob_reference BEFORE INSERT OR UPDATE OF blob_id ON
        meme_template_version_blobs FOR EACH ROW EXECUTE FUNCTION
        al1s_check_blob_reference()""",
    """CREATE TRIGGER check_blob_reference BEFORE INSERT OR UPDATE OF blob_id ON meme_assets
        FOR EACH ROW EXECUTE FUNCTION al1s_check_blob_reference()""",
    """CREATE TABLE meme_send_gates ( platform VARCHAR(16) NOT NULL, available_at TIMESTAMP
        WITH TIME ZONE NOT NULL, owner_receipt_id UUID, PRIMARY KEY (platform),
        CONSTRAINT ck_meme_gate_platform CHECK (platform IN ('qq','discord')), FOREIGN
        KEY(owner_receipt_id) REFERENCES meme_command_receipts (id) ON DELETE SET NULL )""",
    """INSERT INTO meme_send_gates (platform,available_at) VALUES
        ('discord','1970-01-01T00:00:00Z'),('qq','1970-01-01T00:00:00Z')""",
)

SEEDS = json.loads(
    """[
  {
    "id": "bfd27594-fc37-4f97-82f8-000000000001",
    "data": {
      "name": "摸摸",
      "keyword": "摸摸",
      "aliases": [
        "petpet"
      ],
      "enabled": true,
      "random_enabled": true,
      "layout": {
        "schema_version": 1,
        "engine": "petpet",
        "width": 128,
        "height": 128,
        "avatars": [],
        "texts": [],
        "frame_duration_ms": 100,
        "background_color": "#ffffff"
      },
      "background_blob_id": null,
      "foreground_blob_id": null
    }
  },
  {
    "id": "bfd27594-fc37-4f97-82f8-000000000002",
    "data": {
      "name": "图片配字",
      "keyword": "配字",
      "aliases": [
        "caption"
      ],
      "enabled": true,
      "random_enabled": true,
      "layout": {
        "schema_version": 1,
        "engine": "caption",
        "width": 320,
        "height": 320,
        "avatars": [],
        "texts": [],
        "frame_duration_ms": 100,
        "background_color": "#ffffff"
      },
      "background_blob_id": null,
      "foreground_blob_id": null
    }
  },
  {
    "id": "bfd27594-fc37-4f97-82f8-000000000003",
    "data": {
      "name": "圆形头像",
      "keyword": "圆形头像",
      "aliases": [
        "头像圆圈"
      ],
      "enabled": true,
      "random_enabled": true,
      "layout": {
        "schema_version": 1,
        "engine": "composite",
        "width": 320,
        "height": 320,
        "avatars": [
          {
            "x": 32.0,
            "y": 32.0,
            "width": 256.0,
            "height": 256.0,
            "order": 0,
            "shape": "circle",
            "fit": "cover",
            "rotation": 0.0
          }
        ],
        "texts": [],
        "frame_duration_ms": 100,
        "background_color": "#e7eefb"
      },
      "background_blob_id": null,
      "foreground_blob_id": null
    }
  }
]"""
)


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)
    connection = op.get_bind()
    for seed in SEEDS:
        identity, data = UUID(seed["id"]), seed["data"]
        layout = data["layout"]
        engine = layout["engine"]
        avatar_count = 1 if engine == "petpet" else len(layout["avatars"])
        image_count = int(engine == "caption")
        text_count = 1 if engine == "caption" else len(layout["texts"])
        connection.execute(
            text(
                "INSERT INTO meme_templates "
                "(id,name,engine_kind,enabled,random_enabled,builtin,row_version,"
                "created_at,updated_at) "
                "VALUES (:id,:name,:engine,true,true,true,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"id": identity, "name": data["name"], "engine": engine},
        )
        connection.execute(
            text(
                "INSERT INTO meme_template_versions "
                "(id,template_id,version_no,schema_version,min_avatar_count,min_image_count,"
                "min_text_count,layout,created_at) VALUES "
                "(:id,:id,1,1,:avatars,:images,:texts,CAST(:layout AS jsonb),CURRENT_TIMESTAMP)"
            ),
            {
                "id": identity,
                "avatars": avatar_count,
                "images": image_count,
                "texts": text_count,
                "layout": json.dumps(layout),
            },
        )
        connection.execute(
            text("UPDATE meme_templates SET current_version_id=:id WHERE id=:id"), {"id": identity}
        )
        connection.execute(
            text(
                "INSERT INTO meme_template_aliases "
                "(normalized_alias,template_id,is_primary) VALUES (:word,:id,:primary)"
            ),
            [
                {"word": word, "id": identity, "primary": index == 0}
                for index, word in enumerate([data["keyword"], *data["aliases"]])
            ],
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM meme_command_receipts) OR "
            "EXISTS(SELECT 1 FROM meme_assets) OR "
            "EXISTS(SELECT 1 FROM meme_bot_settings) OR EXISTS(SELECT 1 FROM meme_templates "
            "WHERE NOT builtin OR row_version > 1) OR EXISTS(SELECT 1 FROM meme_mutations)"
        )
    ):
        raise RuntimeError("Cannot discard meme templates, settings or interaction receipts")
    op.execute("ALTER TABLE meme_templates DROP CONSTRAINT fk_meme_current_version")
    for table in (
        "meme_send_gates",
        "meme_mutations",
        "meme_command_receipts",
        "meme_bot_scopes",
        "meme_bot_settings",
        "meme_template_version_blobs",
        "meme_template_aliases",
        "meme_template_versions",
        "meme_assets",
        "meme_templates",
    ):
        op.drop_table(table)
