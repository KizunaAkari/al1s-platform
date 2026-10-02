from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class MessageReviewRow(Base):
    __tablename__ = "information_message_reviews"
    __table_args__ = (
        CheckConstraint("state IN ('hold','pending','approved','rejected','expired','completed')",
                        name="ck_information_review_state"),
        CheckConstraint("row_version > 0", name="ck_information_review_version"),
        CheckConstraint("(body_cipher IS NULL) = (key_id IS NULL)",
                        name="ck_information_review_cipher"),
        Index("uq_information_review_source", "service_id", "message_id", unique=True),
        Index("ix_information_review_state", "state", "received_at", "id"),
        Index("ix_information_review_finalized", "finalized_at", "id"),
        Index("ix_information_review_checked", "checked_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("bot_services.id"))
    message_id: Mapped[str] = mapped_column(String(32))
    source_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    body_cipher: Mapped[str | None] = mapped_column(Text)
    key_id: Mapped[str | None] = mapped_column(String(64))
    lexicon_commit: Mapped[str | None] = mapped_column(String(40))
    normalization_version: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("notification_intents.id"), unique=True,
    )
