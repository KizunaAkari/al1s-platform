from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

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
)
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class TerminalArtifactRow(Base):
    __tablename__ = "terminal_artifacts"
    __table_args__ = (
        CheckConstraint(
            "owner_kind IN ('formal_attempt', 'quick_test')",
            name="ck_terminal_artifacts_owner_kind",
        ),
        CheckConstraint(
            "artifact_kind IN ('screenshot', 'video', 'log', 'diagnostic')",
            name="ck_terminal_artifacts_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'ready', 'expired')",
            name="ck_terminal_artifacts_status",
        ),
        CheckConstraint(
            "expected_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_terminal_artifacts_sha256",
        ),
        CheckConstraint("expected_size_bytes >= 0", name="ck_terminal_artifacts_size"),
        CheckConstraint(
            "char_length(file_name) BETWEEN 1 AND 255",
            name="ck_terminal_artifacts_file_name",
        ),
        CheckConstraint(
            "char_length(media_type) BETWEEN 1 AND 255",
            name="ck_terminal_artifacts_media_type",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_terminal_artifacts_idempotency",
        ),
        CheckConstraint("row_version > 0", name="ck_terminal_artifacts_version"),
        CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND blob_id IS NULL) OR "
            "(status = 'ready' AND completed_at IS NOT NULL AND blob_id IS NOT NULL) OR "
            "(status = 'expired' AND completed_at IS NOT NULL AND blob_id IS NULL)",
            name="ck_terminal_artifacts_completion",
        ),
        UniqueConstraint(
            "terminal_id", "idempotency_key", name="uq_terminal_artifacts_idempotency"
        ),
        Index(
            "ix_terminal_artifacts_owner",
            "owner_kind",
            "owner_id",
            "created_at",
            "id",
        ),
        Index("ix_terminal_artifacts_expiry", "status", "expires_at", "id"),
        Index("ix_terminal_artifacts_staging_cleanup", "staging_cleanup_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blob_objects.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staging_cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
