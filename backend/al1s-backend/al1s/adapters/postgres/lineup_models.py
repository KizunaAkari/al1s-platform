from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class LineupRecognitionRow(Base):
    __tablename__ = "lineup_recognitions"
    __table_args__ = (
        CheckConstraint(
            "width > 0 AND height > 0 AND row_version > 0", name="ck_lineup_dimensions"
        ),
        Index("ix_lineup_blob", "blob_id"),
        Index("ix_lineup_history", "created_at", "id"),
        Index("ix_lineup_task_attention", "task_id", "needs_attention", "occurrence_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    blob_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("blob_objects.id"), nullable=False)
    terminal_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("terminals.id"))
    task_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("task_requests.id"))
    occurrence_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("plan_occurrences.id"), unique=True
    )
    retry_source_record_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("lineup_recognitions.id")
    )
    needs_attention: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    attention_reason: Mapped[str] = mapped_column(
        String(30), nullable=False, default="none", server_default="none"
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review: Mapped[list[int | None] | None] = mapped_column(JSONB(none_as_null=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
