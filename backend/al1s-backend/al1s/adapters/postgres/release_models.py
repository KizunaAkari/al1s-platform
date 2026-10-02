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
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class LinuxReleaseRow(Base):
    __tablename__ = "linux_releases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('draft','queued','verifying','published','failed')",
            name="ck_linux_release_state",
        ),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 5368709120", name="ck_linux_release_size"
        ),
        CheckConstraint("sha256 ~ '^[a-f0-9]{64}$'", name="ck_linux_release_hash"),
        CheckConstraint("architecture = 'arm64'", name="ck_linux_release_arch"),
        CheckConstraint("row_version > 0", name="ck_linux_release_version"),
        CheckConstraint(
            "state <> 'published' OR (blob_id IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_linux_release_publication",
        ),
        Index("ix_linux_release_created", "created_at", "id"),
        Index("ix_linux_release_queue", "state", "lease_until", "id"),
        Index("ix_linux_release_blob", "blob_id"),
        Index("ix_linux_release_cleanup", "staging_cleanup_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    version: Mapped[str] = mapped_column(String(64), unique=True)
    architecture: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    candidate_image: Mapped[str] = mapped_column(String(160))
    expected_image_id: Mapped[str] = mapped_column(String(71))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staging_cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    blob_id: Mapped[UUID | None] = mapped_column(ForeignKey("blob_objects.id", ondelete="RESTRICT"))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
