"""Execute accepted paper orders only after receiving later market bars."""

from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic

import structlog

from al1s.adapters.postgres.quant_market_data import QuantMarketData
from al1s.adapters.postgres.quant_orders import QuantOrders
from al1s.quant.book import Fill
from al1s.quant.market_rules import CHINA, check_quote
from al1s.quant.paper_engine import PaperRequest, run_paper, validate_paper_bars
from al1s.quant.provider_routing import PROVIDERS, eligible_sources, select_source
from al1s.quant.source_client import MarketSource


class PaperExecution:
    def __init__(
        self,
        orders: QuantOrders,
        source: MarketSource,
        cache: QuantMarketData,
        *,
        native=run_paper,
        now=lambda: datetime.now(UTC),
    ):
        self.orders, self.source, self.cache, self.native, self.now = (
            orders,
            source,
            cache,
            native,
            now,
        )
        self.log = structlog.get_logger()
        self._stop = Event()
        self._last_poll: dict[tuple[str, str, str], float] = {}
        self._cursor = None

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        pending = self.orders.pending(limit=50, after=self._cursor)
        if not pending:
            self._cursor = None
            return
        with self.source.batch_credentials():
            self._run_batch(pending)

    def _run_batch(self, pending) -> None:
        now = self.now()
        current_day = now.astimezone(CHINA).date()
        deadline = monotonic() + 25
        expiry = monotonic() - 3600
        self._last_poll = {key: value for key, value in self._last_poll.items() if value >= expiry}
        cached = {}
        configured = self.source.configured()
        days = None
        next_day = None
        for order in pending:
            if self._stop.is_set() or monotonic() >= deadline:
                return
            if order.created_at is not None:
                self._cursor = (order.created_at, order.id)
            requested = datetime.fromtimestamp(order.event_time, UTC)
            day = requested.astimezone(CHINA).date()
            if day < current_day:
                self.orders.expire(order.id)
                continue
            if day != current_day:
                continue
            try:
                if days is None:
                    key = day.strftime("%Y%m%d")
                    days = self.source.trading_days(key, key)
                ordered = order.provider_order or (
                    order.provider,
                    *(p for p in PROVIDERS if p != order.provider),
                )
                candidates = eligible_sources(
                    ordered,
                    market=order.market,
                    frequency="1m",
                    adjustment="",
                    configured=configured,
                )

                def fetch(provider):
                    key = (provider, order.market, order.symbol)
                    if key not in cached:
                        tick = monotonic()
                        if tick - self._last_poll.get(key, float("-inf")) < order.refresh_seconds:
                            raise ValueError("quant_source_busy")
                        self._last_poll[key] = tick
                        if len(self._last_poll) > 10000:
                            self._last_poll.pop(next(iter(self._last_poll)))
                        try:
                            cached[key] = self.source.minutes(
                                order.market.lower(), order.symbol, provider=provider
                            )
                        except ValueError as error:
                            cached[key] = error
                    bars = cached[key]
                    if isinstance(bars, ValueError):
                        raise bars
                    bars = [bar for bar in bars if bar.timestamp <= now]
                    if not bars or bars[-1].volume <= 0:
                        raise ValueError("quant_quote_unavailable")
                    check_quote(
                        bars[-1].close,
                        bars[-1].timestamp,
                        self.now(),
                        days,
                        order.quote_max_age_seconds,
                    )
                    later = [
                        bar
                        for bar in bars
                        if requested < bar.timestamp <= now
                        and bar.timestamp.astimezone(CHINA).date() == day
                    ]
                    if len(later) < 3:
                        raise ValueError("quant_source_empty")
                    validate_paper_bars(requested, later[-3:])
                    return bars, later[-3:]

                provider, (bars, later) = select_source(candidates, fetch)
                if next_day is None:
                    future = self.source.trading_days(
                        (day + timedelta(days=1)).strftime("%Y%m%d"),
                        (day + timedelta(days=20)).strftime("%Y%m%d"),
                    )
                    if not future:
                        continue
                    next_day = min(future)
                self.cache.store(
                    order.instrument_id, provider, "1m", "", later, fetched_at=self.now()
                )
            except (ValueError, OSError) as error:
                self.log.warning("quant_market_unavailable", error=type(error).__name__)
                continue
            self._execute(order.id, later, requested, next_day, provider)
        if len(pending) < 50:
            self._cursor = None

    def _execute(self, order_id, bars, requested, next_day, provider=None) -> None:
        try:
            validate_paper_bars(requested, bars)
        except ValueError:
            return
        try:
            snapshot = self.orders.prepare(
                order_id, quote_time=bars[-1].timestamp, execution_provider=provider
            )
        except ValueError as error:
            if str(error) in {"quant_corporate_needs_review", "quant_corporate_data_unavailable"}:
                self.orders.expire(order_id)
            return
        order = snapshot.order
        try:
            request = PaperRequest(
                order.symbol,
                order.side,
                order.quantity,
                requested,
                snapshot.instruments,
                snapshot.cash,
                snapshot.state,
                order.commission_rate,
                order.minimum_commission,
                order.stamp_tax_rate,
                order.slippage,
                tuple(snapshot.positions.items()),
                snapshot.initial_cash,
            )
            result = self.native(request, bars)
            trade_date = result.occurred_at.astimezone(CHINA).date()
            fill = Fill(order.side, result.quantity, result.price, result.fees, trade_date)
            self.orders.complete(
                order.id,
                fill,
                next_trading_day=next_day,
                engine_cash=result.cash,
                engine_position=result.positions[order.symbol],
                engine_state=result.state,
                engine_version="0.3.71",
                occurred_at=result.occurred_at,
            )
        except Exception as error:
            self.log.warning("quant_paper_result_unknown", error=type(error).__name__)
            self.orders.mark_unknown(order.id)
