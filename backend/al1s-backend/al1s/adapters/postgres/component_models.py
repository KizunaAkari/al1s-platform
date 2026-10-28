from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres import execution_models as _execution_models  # noqa: F401
from al1s.adapters.postgres import models as _models  # noqa: F401
from al1s.adapters.postgres.base import Base


class ComponentReleaseRow(Base):
    __tablename__ = "component_releases"
    __table_args__ = (
        UniqueConstraint(
            "component", "version", "architecture", name="uq_component_release_version"
        ),
        CheckConstraint(
            "component IN ('platform','postgresql','s3','mqtt','linux-terminal',"
            "'qq-bot','discord-bot')",
            name="ck_component_release_unit",
        ),
        CheckConstraint(
            "state IN ('draft','queued','verifying','published','failed')",
            name="ck_component_release_state",
        ),
        CheckConstraint("architecture IN ('amd64','arm64')", name="ck_component_release_arch"),
        CheckConstraint(
            "source_kind <> 'archive' OR (size_bytes > 0 AND size_bytes <= 5368709120)",
            name="ck_component_release_size",
        ),
        CheckConstraint(
            "source_kind <> 'archive' OR sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_component_release_hash",
        ),
        CheckConstraint("row_version > 0", name="ck_component_release_row_version"),
        CheckConstraint(
            "state <> 'published' OR (published_at IS NOT NULL AND candidate_image IS NOT NULL "
            "AND expected_image_id IS NOT NULL "
            "AND (source_kind='registry' OR blob_id IS NOT NULL))",
            name="ck_component_release_published",
        ),
        Index("ix_component_release_page", "component", "created_at", "id"),
        Index("ix_component_release_queue", "state", "lease_until", "id"),
        Index("ix_component_release_blob", "blob_id"),
        CheckConstraint(
            "source_kind IN ('archive','registry')", name="ck_component_release_source"
        ),
        CheckConstraint(
            "(source_kind='archive' AND size_bytes IS NOT NULL AND sha256 IS NOT NULL "
            "AND candidate_image IS NOT NULL AND expected_image_id IS NOT NULL) OR "
            "(source_kind='registry' AND component='qq-bot' "
            "AND size_bytes IS NULL AND sha256 IS NULL "
            "AND blob_id IS NULL AND registry_digest ~ '^sha256:[a-f0-9]{64}$' "
            "AND platform_manifest_digest ~ '^sha256:[a-f0-9]{64}$' "
            "AND runtime_artifact_digest ~ '^sha256:[a-f0-9]{64}$' AND runtime_version IS NOT NULL "
            "AND registry_digest IS NOT NULL AND platform_manifest_digest IS NOT NULL "
            "AND runtime_artifact_digest IS NOT NULL)",
            name="ck_component_release_source_identity",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    component: Mapped[str] = mapped_column(String(32))
    version: Mapped[str] = mapped_column(String(64))
    architecture: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    candidate_image: Mapped[str | None] = mapped_column(String(256))
    expected_image_id: Mapped[str | None] = mapped_column(String(71))
    source_kind: Mapped[str] = mapped_column(
        String(16), server_default="archive", default="archive"
    )
    registry_digest: Mapped[str | None] = mapped_column(String(71))
    platform_manifest_digest: Mapped[str | None] = mapped_column(String(71))
    runtime_artifact_digest: Mapped[str | None] = mapped_column(String(71))
    runtime_version: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(String(256))
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_notes: Mapped[str | None] = mapped_column(String(4000))
    native_version: Mapped[str] = mapped_column(String(64))
    compatible_from: Mapped[str] = mapped_column(String(128))
    data_policy: Mapped[str] = mapped_column(String(32))
    schema_from: Mapped[str | None] = mapped_column(String(64))
    schema_to: Mapped[str | None] = mapped_column(String(64))
    maa_version: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    blob_id: Mapped[UUID | None] = mapped_column(ForeignKey("blob_objects.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class ComponentOperationRow(Base):
    __tablename__ = "component_operations"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_component_operation_key"),
        CheckConstraint(
            "state IN ('accepted','preparing','switching','verifying',"
            "'succeeded','failed_safe','unknown')",
            name="ck_component_operation_state",
        ),
        CheckConstraint(
            "(scope = 'platform' AND terminal_id IS NULL "
            "AND component NOT IN ('linux-terminal','qq-bot','discord-bot')) OR "
            "(scope = 'terminal:' || terminal_id::text AND terminal_id IS NOT NULL "
            "AND component = 'linux-terminal') OR "
            "(scope='bot:qq' AND terminal_id IS NULL AND component='qq-bot') OR "
            "(scope='bot:discord' AND terminal_id IS NULL AND component='discord-bot')",
            name="ck_component_operation_target",
        ),
        CheckConstraint("row_version > 0", name="ck_component_operation_version"),
        CheckConstraint(
            "component IN ('platform','postgresql','s3','mqtt','linux-terminal',"
            "'qq-bot','discord-bot')",
            name="ck_component_operation_unit",
        ),
        CheckConstraint("expected_source ~ '^[a-f0-9]{64}$'", name="ck_component_operation_source"),
        Index("ix_component_operation_page", "created_at", "id"),
        Index(
            "uq_component_operation_active_scope",
            "scope",
            unique=True,
            postgresql_where=text("state NOT IN ('succeeded','failed_safe')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    component: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(64))
    terminal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("terminals.id", ondelete="RESTRICT")
    )
    release_id: Mapped[UUID] = mapped_column(
        ForeignKey("component_releases.id", ondelete="RESTRICT")
    )
    expected_source: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    recovery_point: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
