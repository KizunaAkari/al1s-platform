from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class AiConversationRow(Base):
    __tablename__ = 'bot_ai_conversations'
    __table_args__ = (
        CheckConstraint("scope_kind IN ('qq_group','qq_private')", name='ck_ai_history_scope'),
        CheckConstraint('revision > 0 AND next_turn > 0', name='ck_ai_history_order'),
        CheckConstraint('deleted_at IS NULL OR NOT current', name='ck_ai_history_deleted'),
        UniqueConstraint('service_id', 'legacy_id', name='uq_ai_history_legacy'),
        Index('uq_ai_history_current', 'service_id', 'scope_kind', 'target_id', unique=True,
              postgresql_where=text('current'), sqlite_where=text('current')),
        Index('ix_ai_history_export', 'service_id', 'id'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[UUID] = mapped_column(ForeignKey('bot_services.id', ondelete='RESTRICT'))
    scope_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(20))
    current: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    next_turn: Mapped[int] = mapped_column(Integer, default=1)
    legacy_id: Mapped[str | None] = mapped_column(String(128))
    legacy_create_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    import_digest: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    persona_id: Mapped[str | None] = mapped_column(String(128))
    token_usage: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiTurnRow(Base):
    __tablename__ = 'bot_ai_turns'
    __table_args__ = (
        UniqueConstraint('conversation_id', 'sequence', name='uq_ai_turn_order'),
        CheckConstraint('sequence > 0', name='ck_ai_turn_order'),
    )
    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey('bot_ai_conversations.id', ondelete='CASCADE'))
    sequence: Mapped[int] = mapped_column(Integer)
    content_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AiMessageRow(Base):
    __tablename__ = 'bot_ai_messages'
    __table_args__ = (
        CheckConstraint('ordinal >= 0', name='ck_ai_message_order'),
        CheckConstraint("role IN ('user','assistant','tool','system')", name='ck_ai_message_role'),
    )
    turn_id: Mapped[UUID] = mapped_column(ForeignKey('bot_ai_turns.request_id', ondelete='CASCADE'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)


class AiVisionJobRow(Base):
    __tablename__ = 'bot_ai_vision_jobs'
    __table_args__ = (
        CheckConstraint("(boot_id IS NULL AND execution_hash IS NULL AND registration_state IS NULL AND NOT transport_started) OR "
                        "(boot_id IS NOT NULL AND execution_hash IS NOT NULL AND registration_state IS NOT NULL "
                        "AND registration_state IN ('pending','confirmed','unknown','rejected') "
                        "AND (NOT transport_started OR registration_state <> 'pending') "
                        "AND (registration_state <> 'rejected' OR transport_started))", name='ck_ai_vision_resource_state'),
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey('bot_services.id', ondelete='RESTRICT'), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(1024), primary_key=True)
    prompt_id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    boot_id: Mapped[str | None] = mapped_column(String(64))
    execution_hash: Mapped[str | None] = mapped_column(String(64))
    registration_state: Mapped[str | None] = mapped_column(String(16))
    transport_started: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'))
