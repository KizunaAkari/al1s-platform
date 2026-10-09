from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class MemeTemplateRow(Base):
    __tablename__ = "meme_templates"
    __table_args__ = (
        CheckConstraint("engine_kind IN ('petpet','caption','composite')", name="ck_meme_engine"),
        CheckConstraint("row_version > 0", name="ck_meme_template_version"),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["meme_template_versions.template_id", "meme_template_versions.id"],
            use_alter=True,
            name="fk_meme_current_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_meme_template_page", "deleted_at", "id"),
        Index("ix_meme_random", "enabled", "random_enabled", "deleted_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    engine_kind: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean)
    random_enabled: Mapped[bool] = mapped_column(Boolean)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    row_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemeVersionRow(Base):
    __tablename__ = "meme_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version_no", name="uq_meme_version_number"),
        UniqueConstraint("template_id", "id", name="uq_meme_version_owner"),
        CheckConstraint("version_no > 0 AND schema_version = 1", name="ck_meme_schema_version"),
        CheckConstraint(
            "min_avatar_count BETWEEN 0 AND 4 AND min_image_count BETWEEN 0 AND 1 "
            "AND min_text_count BETWEEN 0 AND 4",
            name="ck_meme_inputs",
        ),
        Index("ix_meme_version_cleanup", "lease_until", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    template_id: Mapped[UUID] = mapped_column(ForeignKey("meme_templates.id", ondelete="RESTRICT"))
    version_no: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    min_avatar_count: Mapped[int] = mapped_column(Integer)
    min_image_count: Mapped[int] = mapped_column(Integer)
    min_text_count: Mapped[int] = mapped_column(Integer)
    layout: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemeAliasRow(Base):
    __tablename__ = "meme_template_aliases"
    __table_args__ = (Index("ix_meme_alias_template", "template_id"),)
    normalized_alias: Mapped[str] = mapped_column(String(32), primary_key=True)
    template_id: Mapped[UUID] = mapped_column(ForeignKey("meme_templates.id", ondelete="RESTRICT"))
    is_primary: Mapped[bool] = mapped_column(Boolean)


class MemeVersionBlobRow(Base):
    __tablename__ = "meme_template_version_blobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["blob_id"], ["meme_assets.blob_id"], name="fk_meme_blob_owned", ondelete="RESTRICT"
        ),
        CheckConstraint("role IN ('background','foreground')", name="ck_meme_blob_role"),
        Index("ix_meme_blob_reference", "blob_id"),
    )
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("meme_template_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    blob_id: Mapped[UUID] = mapped_column(ForeignKey("blob_objects.id", ondelete="RESTRICT"))


class MemeAssetRow(Base):
    __tablename__ = "meme_assets"
    __table_args__ = (
        CheckConstraint(
            "width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 4096 "
            "AND width::bigint * height <= 8000000",
            name="ck_meme_asset_dimensions",
        ),
        Index("ix_meme_asset_cleanup", "expires_at", "blob_id"),
    )
    blob_id: Mapped[UUID] = mapped_column(
        ForeignKey("blob_objects.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemeSettingsRow(Base):
    __tablename__ = "meme_bot_settings"
    __table_args__ = (
        CheckConstraint("cooldown_seconds BETWEEN 1 AND 60", name="ck_meme_cooldown"),
        CheckConstraint("row_version > 0", name="ck_meme_settings_version"),
    )
    bot_service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean)
    private_enabled: Mapped[bool] = mapped_column(Boolean)
    qq_event_ws_url: Mapped[str | None] = mapped_column(String(1024))
    cooldown_seconds: Mapped[int] = mapped_column(Integer)
    row_version: Mapped[int] = mapped_column(Integer)


class MemeScopeRow(Base):
    __tablename__ = "meme_bot_scopes"
    __table_args__ = (
        CheckConstraint("scope_kind IN ('qq_group','discord_channel')", name="ck_meme_scope_kind"),
    )
    bot_service_id: Mapped[UUID] = mapped_column(
        ForeignKey("meme_bot_settings.bot_service_id", ondelete="CASCADE"),
        primary_key=True,
    )
    scope_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    guild_id: Mapped[str | None] = mapped_column(String(20))


class MemeReceiptRow(Base):
    __tablename__ = "meme_command_receipts"
    __table_args__ = (
        UniqueConstraint("bot_service_id", "source_message_id", name="uq_meme_source_message"),
        CheckConstraint(
            "status IN ('claimed','send_started','sent','failed','unknown')",
            name="ck_meme_receipt_status",
        ),
        CheckConstraint(
            "scope_kind IN ('qq_group','qq_private','discord_channel','discord_private')",
            name="ck_meme_receipt_scope",
        ),
        Index("ix_meme_receipt_cleanup", "expires_at", "id"),
        Index("ix_meme_receipt_version", "template_version_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    bot_service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"))
    source_message_id: Mapped[str] = mapped_column(String(64))
    scope_kind: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(20))
    guild_id: Mapped[str | None] = mapped_column(String(20))
    template_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("meme_template_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemeMutationRow(Base):
    __tablename__ = "meme_mutations"
    __table_args__ = (
        CheckConstraint(
            "(template_id IS NOT NULL)::int + (bot_service_id IS NOT NULL)::int = 1",
            name="ck_meme_mutation_owner",
        ),
        Index("ix_meme_mutation_cleanup", "expires_at", "key"),
        Index("ix_meme_mutation_version", "version_id"),
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    template_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("meme_templates.id", ondelete="RESTRICT"),
        nullable=True,
    )
    bot_service_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"),
        nullable=True,
    )
    version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("meme_template_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemeSendGateRow(Base):
    __tablename__ = "meme_send_gates"
    __table_args__ = (
        CheckConstraint("platform IN ('qq','discord')", name="ck_meme_gate_platform"),
    )
    platform: Mapped[str] = mapped_column(String(16), primary_key=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    owner_receipt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("meme_command_receipts.id", ondelete="SET NULL"),
        nullable=True,
    )
