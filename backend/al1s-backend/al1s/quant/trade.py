"""Validate a QQ manual order before persisting it for paper execution."""

from datetime import UTC, datetime
from uuid import UUID

from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_instruments import QuantInstruments, classify, infer_market
from al1s.adapters.postgres.quant_market_data import QuantMarketData
from al1s.adapters.postgres.quant_orders import QuantOrders
from al1s.quant.commands import QuantCommand
from al1s.quant.corporate_service import CorporateService
from al1s.quant.market_rules import CHINA, check_order_quantity, check_quote
from al1s.quant.provider_routing import PROVIDER_NAMES, eligible_sources, select_source
from al1s.quant.source_client import MarketSource


class QuantTrade:
    def __init__(
        self,
        source: MarketSource,
        accounts: QuantAccounts,
        instruments: QuantInstruments,
        orders: QuantOrders,
        cache: QuantMarketData,
        *,
        corporate: CorporateService,
        now=lambda: datetime.now(UTC),
    ):
        self.source, self.accounts, self.instruments, self.orders, self.cache = (
            source,
            accounts,
            instruments,
            orders,
            cache,
        )
        self.now = now
        self.corporate = corporate

    def submit(self, service_id: UUID, command: QuantCommand) -> str:
        settings = command.settings
        if settings is None or len(command.arguments) != 2 or command.action not in {"buy", "sell"}:
            raise ValueError("quant_order_invalid")
        code, raw_quantity = command.arguments
        if not raw_quantity.isascii() or not raw_quantity.isdecimal() or len(raw_quantity) > 9:
            raise ValueError("quant_quantity_invalid")
        quantity = int(raw_quantity)
        now = self.now()
        if now.tzinfo is None or not 0 <= now.timestamp() - command.event_timestamp <= 120:
            raise ValueError("quant_event_stale")
        account = self.accounts.get(command.source.sender_id)
        if account is None:
            raise ValueError("quant_account_not_found")
        if account.state != "active":
            raise ValueError("quant_account_needs_review")
        day = now.astimezone(CHINA).date()
        market = infer_market(code)
        current, board = classify(market, code, day)
        days = self.source.trading_days(day.strftime("%Y%m%d"), day.strftime("%Y%m%d"))
        candidates = eligible_sources(
            settings.provider_order,
            market=market,
            frequency="1m",
            adjustment="",
            configured=self.source.configured(),
        )

        def fetch(provider):
            found = self.source.minutes(market.lower(), current, provider=provider)
            observed = self.now()
            found = [bar for bar in found if bar.timestamp <= observed]
            if not found or found[-1].volume <= 0:
                raise ValueError("quant_quote_unavailable")
            latest = found[-1]
            if (
                latest.high < max(latest.open, latest.close)
                or latest.low > min(latest.open, latest.close)
                or latest.high < latest.low
            ):
                raise ValueError("quant_quote_unavailable")
            check_quote(
                found[-1].close,
                found[-1].timestamp,
                observed,
                days,
                settings.max_quote_age_seconds,
            )
            return found

        provider, bars = select_source(candidates, fetch)
        latest = bars[-1]
        checked_at = self.now()
        if (
            checked_at.tzinfo is None
            or not 0 <= checked_at.timestamp() - command.event_timestamp <= 120
        ):
            raise ValueError("quant_event_stale")
        check_quote(
            latest.close, latest.timestamp, checked_at, days, settings.max_quote_age_seconds
        )
        instrument = self.instruments.register(market, current, day)
        self.corporate.ensure(instrument, account)
        available = (
            self.accounts.available_for(command.source.sender_id, instrument.id, day)
            if command.action == "sell"
            else 0
        )
        check_order_quantity(board, command.action, quantity, available)
        self.cache.store(instrument.id, provider, "1m", "", bars[-1:], fetched_at=checked_at)
        final_now = self.now()
        if (
            final_now.tzinfo is None
            or not 0 <= final_now.timestamp() - command.event_timestamp <= 120
        ):
            raise ValueError("quant_event_stale")
        check_quote(latest.close, latest.timestamp, final_now, days, settings.max_quote_age_seconds)
        order, created = self.orders.accept(
            service_id,
            command,
            instrument.id,
            quantity,
            quote_time=latest.timestamp,
            quote_price=latest.close,
            quote_provider=provider,
        )
        state = "已受理" if created else "已存在"
        display_provider = order.provider or provider
        display_time = order.quote_time or latest.timestamp
        display_price = order.quote_price if order.quote_price is not None else latest.close
        return (
            f"模拟委托 {str(order.id)[:8]} {state}，状态 {order.status}；"
            f"行情 {display_price} 元，时间 {display_time:%Y-%m-%d %H:%M:%S}，"
            f"来源 {PROVIDER_NAMES[display_provider]}。"
            f"佣金比例 {settings.commission_rate}，最低佣金 {settings.minimum_commission}，"
            f"卖出税 {settings.stamp_tax_rate}，滑点 {settings.slippage}。"
        )
