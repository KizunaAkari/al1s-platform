"""Persisted batch ownership and separately versioned human annotations."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class LineupBatchRow(Base):
    __tablename__ = "lineup_batches"
    __table_args__ = (CheckConstraint("item_count BETWEEN 1 AND 200", name="ck_lineup_batch_size"),)
    task_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("task_requests.id"), primary_key=True)
    retry_source_task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task_requests.id"), index=True
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class LineupRequestRow(Base):
    __tablename__ = "lineup_submission_receipts"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("task_requests.id"), nullable=False, index=True
    )


class LineupAnnotationRow(Base):
    __tablename__ = "lineup_annotations"
    __table_args__ = (
        CheckConstraint(
            "version > 0 AND state IN ('draft','confirmed')", name="ck_lineup_annotation"
        ),
        CheckConstraint(
            "attack_size BETWEEN 0 AND 6 AND defense_size BETWEEN 0 AND 6",
            name="ck_lineup_annotation_sizes",
        ),
    )
    record_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("lineup_recognitions.id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    attack_size: Mapped[int] = mapped_column(Integer, nullable=False)
    defense_size: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LineupAnnotationSlotRow(Base):
    __tablename__ = "lineup_annotation_slots"
    __table_args__ = (
        CheckConstraint(
            "side IN ('attack','defense') AND slot_index BETWEEN 0 AND 5",
            name="ck_lineup_annotation_slot",
        ),
    )
    record_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("lineup_annotations.record_id"), primary_key=True
    )
    side: Mapped[str] = mapped_column(String(10), primary_key=True)
    slot_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int | None] = mapped_column(Integer)


class LineupAnnotationRegionRow(Base):
    __tablename__ = "lineup_annotation_regions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["record_id", "side", "slot_index"],
            [
                "lineup_annotation_slots.record_id",
                "lineup_annotation_slots.side",
                "lineup_annotation_slots.slot_index",
            ],
        ),
        CheckConstraint(
            "kind IN ('portrait','name') AND x >= 0 AND y >= 0 AND width > 0 AND height > 0",
            name="ck_lineup_annotation_region",
        ),
    )
    record_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    side: Mapped[str] = mapped_column(String(10), primary_key=True)
    slot_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
