from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class BotAiSettingsRow(Base):
    __tablename__ = "bot_ai_settings"
    __table_args__ = (
        CheckConstraint("desired_version > 0 AND applied_version >= 0", name="ck_bot_ai_version"),
        CheckConstraint("tts_mode IN ('off','voice_input','always')", name="ck_bot_ai_tts_mode"),
        CheckConstraint("smart_style IN ('context','random')", name="ck_bot_ai_smart_style"),
        CheckConstraint("smart_min_interval_seconds BETWEEN 0 AND 3600 AND "
                        "smart_cooldown_seconds BETWEEN 0 AND 3600 AND "
                        "smart_window_seconds BETWEEN 1 AND 3600 AND "
                        "smart_max_replies BETWEEN 1 AND 50", name="ck_bot_ai_smart_limits"),
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    event_ws_url: Mapped[str | None] = mapped_column(String(1024))
    allow_private: Mapped[bool] = mapped_column(Boolean, default=False)
    vision_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    stt_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tts_mode: Mapped[str] = mapped_column(String(16), default="off")
    persona: Mapped[str] = mapped_column(Text, default="")
    wake_words: Mapped[list] = mapped_column(JSONB, default=list)
    desired_version: Mapped[int] = mapped_column(Integer, default=1)
    applied_version: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(64))
    smart_style: Mapped[str] = mapped_column(String(16), default="context", server_default="context")
    smart_min_interval_seconds: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    smart_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    smart_window_seconds: Mapped[int] = mapped_column(Integer, default=120, server_default="120")
    smart_max_replies: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    smart_voice_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    smart_stickers_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    smart_collect_stickers: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class BotAiScopeRow(Base):
    __tablename__ = "bot_ai_scopes"
    __table_args__ = (
        CheckConstraint("scope_kind = 'qq_group'", name="ck_bot_ai_scope"),
        CheckConstraint("NOT (reply_all AND smart_reply)", name="ck_bot_ai_reply_mode"),
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_ai_settings.service_id", ondelete="CASCADE"), primary_key=True
    )
    scope_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    reply_all: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    smart_reply: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class BotAiProviderRow(Base):
    __tablename__ = "bot_ai_providers"
    __table_args__ = (
        CheckConstraint("role IN ('chat','vision','stt','tts')", name="ck_bot_ai_provider_role"),
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_ai_settings.service_id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    adapter_kind: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(1024), default="")
    secret_id: Mapped[UUID | None] = mapped_column(ForeignKey("encrypted_secrets.id"))
    voice: Mapped[str] = mapped_column(String(200), default="")
    options: Mapped[dict] = mapped_column(JSONB, default=dict)


class BotAiChatProfileRow(Base):
    __tablename__ = "bot_ai_chat_profiles"
    __table_args__ = (
        UniqueConstraint("service_id", "id", name="uq_bot_ai_chat_profile_service_id"),
        UniqueConstraint("service_id", "normalized_name", name="uq_bot_ai_chat_profile_name"),
        CheckConstraint("network_mode IN ('direct','proxy')", name="ck_bot_ai_chat_profile_network_mode"),
        Index("uq_bot_ai_chat_default", "service_id", unique=True,
              postgresql_where=text("is_default"), sqlite_where=text("is_default = 1")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_ai_settings.service_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    persona: Mapped[str] = mapped_column(Text, nullable=False, default="")
    adapter_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    network_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="direct",
                                              server_default=text("'direct'"))
    secret_id: Mapped[UUID | None] = mapped_column(ForeignKey("encrypted_secrets.id"))
    supports_vision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class BotAiSessionProfileRow(Base):
    __tablename__ = "bot_ai_session_profiles"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "profile_id"],
                             ["bot_ai_chat_profiles.service_id", "bot_ai_chat_profiles.id"],
                             ondelete="RESTRICT"),
        CheckConstraint("scope_kind IN ('qq_group','qq_private')", name="ck_bot_ai_session_profile_scope"),
        Index("ix_bot_ai_session_profile", "service_id", "profile_id"),
    )

    service_id: Mapped[UUID] = mapped_column(primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotAiRequestRow(Base):
    __tablename__ = "bot_ai_requests"
    __table_args__ = (
        UniqueConstraint("service_id", "source_message_id", name="uq_bot_ai_source_message"),
        CheckConstraint("scope_kind IN ('qq_group','qq_private')", name="ck_bot_ai_request_scope"),
        CheckConstraint(
            "status IN ('queued','running','stopping','blocked','completed','failed',"
            "'expired','interrupted','unknown')", name="ck_bot_ai_request_status"
        ),
        CheckConstraint("send_status IN ('none','ready','sending','sent','failed','unknown')",
                        name="ck_bot_ai_send_status"),
        CheckConstraint("sent_count BETWEEN 0 AND 5", name="ck_bot_ai_sent_count"),
        Index("ix_bot_ai_expiry", "status", "expires_at", "id"),
        Index("ix_bot_ai_service_time", "service_id", "created_at", "id"),
        Index(
            "uq_bot_ai_active_session", "service_id", "scope_kind", "target_id", unique=True,
            postgresql_where=text("status IN ('running','stopping','blocked')"),
            sqlite_where=text("status IN ('running','stopping','blocked')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id"))
    scope_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(20))
    source_message_id: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    send_status: Mapped[str] = mapped_column(String(16))
    epoch: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_code: Mapped[str | None] = mapped_column(String(64))
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    meme_receipt_id: Mapped[UUID | None] = mapped_column(ForeignKey("meme_command_receipts.id"))
