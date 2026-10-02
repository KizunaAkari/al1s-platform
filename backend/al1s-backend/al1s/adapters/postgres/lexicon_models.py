from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class LexiconVersionRow(Base):
    __tablename__ = "information_lexicon_versions"
    __table_args__ = (
        Index("uq_information_lexicon_active", "active", unique=True,
              postgresql_where=text("active")),
        Index("uq_information_lexicon_version", "commit", "normalization_version", unique=True),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    commit: Mapped[str] = mapped_column(String(40))
    sha256: Mapped[str] = mapped_column(String(64))
    normalization_version: Mapped[str] = mapped_column(String(100))
    license_text: Mapped[str] = mapped_column(Text)
    raw: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
