from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class TradingReplyTemplateRow(Base):
    __tablename__ = "trading_reply_templates"
    __table_args__ = (
        CheckConstraint("engine IN ('quant','freqtrade')", name="ck_trading_reply_engine"),
        CheckConstraint("length(template) BETWEEN 1 AND 4096", name="ck_trading_reply_length"),
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="CASCADE"), primary_key=True
    )
    engine: Mapped[str] = mapped_column(String(16), primary_key=True)
    action: Mapped[str] = mapped_column(String(16), primary_key=True)
    variant: Mapped[str] = mapped_column(String(32), primary_key=True)
    template: Mapped[str] = mapped_column(Text)


def load_templates(session, engine, service_ids):
    if not service_ids:
        return {}
    if len(service_ids) > 100 or engine not in {"quant", "freqtrade"}:
        raise ValueError("reply_scope_invalid")
    rows = session.scalars(
        select(TradingReplyTemplateRow)
        .where(
            TradingReplyTemplateRow.engine == engine,
            TradingReplyTemplateRow.service_id.in_(service_ids),
        )
        .limit(len(service_ids) * 128)
    ).all()
    result = {}
    for row in rows:
        result.setdefault(row.service_id, {}).setdefault(row.action, {})[row.variant] = row.template
    return result
