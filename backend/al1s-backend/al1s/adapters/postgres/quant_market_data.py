"""Shared, bounded market cache, keyed by source, adjustment and time."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.quant_models import QuantBarRow, QuantQuoteRow
from al1s.quant.market_data import MarketBar
from al1s.quant.provider_routing import PROVIDERS


class QuantMarketData:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _key(provider: str, frequency: str, adjustment: str) -> None:
        if (
            provider not in PROVIDERS
            or frequency not in {"1m", "1d"}
            or adjustment not in {"", "qfq", "hfq"}
            or (frequency == "1m" and adjustment != "")
        ):
            raise ValueError("quant_cache_key_invalid")

    def store(
        self,
        instrument_id: UUID,
        provider: str,
        frequency: str,
        adjustment: str,
        bars: list[MarketBar],
        *,
        fetched_at: datetime,
    ) -> None:
        self._key(provider, frequency, adjustment)
        if (
            fetched_at.tzinfo is None
            or len(bars) > 8000
            or len({bar.timestamp for bar in bars}) != len(bars)
        ):
            raise ValueError("quant_cache_bars_invalid")
        for bar in bars:
            if (
                bar.timestamp.tzinfo is None
                or bar.timestamp > fetched_at
                or type(bar.volume) is not int
                or bar.volume < 0
                or any(
                    not isinstance(p, Decimal)
                    or not p.is_finite()
                    or p <= 0
                    or p != p.quantize(Decimal("0.000001"))
                    for p in (bar.open, bar.high, bar.low, bar.close)
                )
                or bar.high < max(bar.open, bar.close)
                or bar.low > min(bar.open, bar.close)
            ):
                raise ValueError("quant_cache_bars_invalid")
        with self.sessions.begin() as session:
            for start in range(0, len(bars), 500):
                batch = bars[start : start + 500]
                stmt = insert(QuantBarRow).values(
                    [
                        {
                            "id": uuid4(),
                            "instrument_id": instrument_id,
                            "provider": provider,
                            "frequency": frequency,
                            "adjustment": adjustment,
                            "bar_time": bar.timestamp,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "fetched_at": fetched_at,
                        }
                        for bar in batch
                    ]
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[
                            QuantBarRow.instrument_id,
                            QuantBarRow.provider,
                            QuantBarRow.frequency,
                            QuantBarRow.adjustment,
                            QuantBarRow.bar_time,
                        ],
                        set_={
                            name: getattr(stmt.excluded, name)
                            for name in ("open", "high", "low", "close", "volume", "fetched_at")
                        },
                        where=stmt.excluded.fetched_at >= QuantBarRow.fetched_at,
                    )
                )
            if bars and frequency == "1m" and adjustment == "":
                latest = max(bars, key=lambda bar: bar.timestamp)
                stmt = insert(QuantQuoteRow).values(
                    instrument_id=instrument_id,
                    provider=provider,
                    price=latest.close,
                    quote_time=latest.timestamp,
                    fetched_at=fetched_at,
                )
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[QuantQuoteRow.instrument_id, QuantQuoteRow.provider],
                        set_={
                            name: getattr(stmt.excluded, name)
                            for name in ("price", "quote_time", "fetched_at")
                        },
                        where=(stmt.excluded.quote_time > QuantQuoteRow.quote_time)
                        | (
                            (stmt.excluded.quote_time == QuantQuoteRow.quote_time)
                            & (stmt.excluded.fetched_at >= QuantQuoteRow.fetched_at)
                        ),
                    )
                )

    def read(
        self,
        instrument_id: UUID,
        provider: str,
        frequency: str,
        adjustment: str,
        *,
        start: datetime,
        end: datetime,
        limit: int = 8000,
    ) -> list[MarketBar]:
        self._key(provider, frequency, adjustment)
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start > end
            or type(limit) is not int
            or not 1 <= limit <= 8000
        ):
            raise ValueError("quant_cache_range_invalid")
        with self.sessions() as session:
            rows = session.execute(
                select(
                    QuantBarRow.bar_time,
                    QuantBarRow.open,
                    QuantBarRow.high,
                    QuantBarRow.low,
                    QuantBarRow.close,
                    QuantBarRow.volume,
                )
                .where(
                    QuantBarRow.instrument_id == instrument_id,
                    QuantBarRow.provider == provider,
                    QuantBarRow.frequency == frequency,
                    QuantBarRow.adjustment == adjustment,
                    QuantBarRow.bar_time >= start,
                    QuantBarRow.bar_time <= end,
                )
                .order_by(QuantBarRow.bar_time)
                .limit(limit)
            ).all()
            return [MarketBar(*row) for row in rows]

    def cleanup_minutes(self, before: datetime, *, limit: int = 10000) -> int:
        if before.tzinfo is None or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("quant_cache_range_invalid")
        with self.sessions.begin() as session:
            ids = select(QuantBarRow.id).where(
                QuantBarRow.frequency == "1m", QuantBarRow.bar_time < before
            )
            ids = ids.order_by(QuantBarRow.bar_time, QuantBarRow.id).limit(limit)
            result = session.execute(delete(QuantBarRow).where(QuantBarRow.id.in_(ids)))
            return result.rowcount
