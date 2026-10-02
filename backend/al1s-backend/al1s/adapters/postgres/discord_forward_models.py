from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class DiscordForwardRuleRow(Base):
    __tablename__ = "discord_forward_rules"
    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('contains','acrostic','frequency')", name="ck_discord_forward_trigger"
        ),
        CheckConstraint("row_version > 0", name="ck_discord_forward_version"),
        CheckConstraint(
            "frequency_count IS NULL OR frequency_count BETWEEN 2 AND 64",
            name="ck_discord_forward_count",
        ),
        CheckConstraint(
            "frequency_window_seconds IS NULL OR frequency_window_seconds BETWEEN 1 AND 3600",
            name="ck_discord_forward_window",
        ),
        CheckConstraint(
            "cooldown_seconds IS NULL OR cooldown_seconds BETWEEN 0 AND 3600",
            name="ck_discord_forward_cooldown",
        ),
        Index("ix_discord_forward_service", "bot_service_id", "created_at", "id"),
        Index("ix_discord_forward_source", "bot_service_id", "guild_id", "channel_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    bot_service_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bot_services.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_text: Mapped[str | None] = mapped_column(String(256))
    frequency_count: Mapped[int | None] = mapped_column(Integer)
    frequency_window_seconds: Mapped[int | None] = mapped_column(Integer)
    cooldown_seconds: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscordForwardActionRow(Base):
    __tablename__ = "discord_forward_actions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('qq_private','qq_group','smtp')", name="ck_discord_forward_action_kind"
        ),
        CheckConstraint(
            "review_policy IN ('none','lexicon')", name="ck_discord_forward_action_policy"
        ),
        CheckConstraint(
            "kind <> 'qq_group' OR review_policy = 'lexicon'",
            name="ck_discord_forward_group_review",
        ),
        CheckConstraint(
            "kind <> 'smtp' OR review_policy = 'none'", name="ck_discord_forward_smtp_review"
        ),
        Index("ix_discord_forward_action_rule", "rule_id", "position", "id"),
        Index("ix_discord_forward_action_channel", "channel_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    rule_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discord_forward_rules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("notification_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    review_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
