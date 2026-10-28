"""QQ request parsing and validated fixed-strategy historical backtests."""

from datetime import UTC, date, datetime, time, timedelta
from threading import BoundedSemaphore
from uuid import UUID

from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_backtests import QuantBacktests
from al1s.adapters.postgres.quant_instruments import QuantInstruments, classify, infer_market
from al1s.adapters.postgres.quant_market_data import QuantMarketData
from al1s.quant.backtest_engine import BacktestRequest, run_backtest
from al1s.quant.bse_mapping import SWITCH_DATE, code_for_date
from al1s.quant.commands import QuantCommand
from al1s.quant.market_data import normalize_auction_bars, validate_bars
from al1s.quant.market_rules import CHINA
from al1s.quant.provider_routing import PROVIDER_NAMES, eligible_sources, select_source
from al1s.quant.source_client import MarketSource


class QuantBacktest:
    def __init__(
        self,
        source: MarketSource,
        accounts: QuantAccounts,
        instruments: QuantInstruments,
        cache: QuantMarketData,
        repository: QuantBacktests,
        *,
        native=run_backtest,
        now=lambda: datetime.now(UTC),
    ):
        self.source, self.accounts, self.instruments, self.cache, self.repository = (
            source,
            accounts,
            instruments,
            cache,
            repository,
        )
        self.native, self.now = native, now
        self._slots = BoundedSemaphore(2)

    def submit(self, service_id: UUID, command: QuantCommand) -> str:
        settings, args = command.settings, list(command.arguments)
        if settings is None or not 4 <= len(args) <= 8:
            raise ValueError("quant_backtest_invalid")
        strategy = {"买入持有": "buy_hold", "双均线": "dual_ma"}.get(args[0])
        if strategy is None:
            raise ValueError("quant_strategy_invalid")
        try:
            start, end = date.fromisoformat(args[2]), date.fromisoformat(args[3])
        except ValueError as error:
            raise ValueError("quant_backtest_invalid") from error
        now = self.now()
        local = now.astimezone(CHINA)
        if (
            start > end
            or end > local.date()
            or (end == local.date() and local.time() < time(15, 5))
        ):
            raise ValueError("quant_backtest_range_invalid")
        if self.accounts.get(command.source.sender_id) is None:
            raise ValueError("quant_account_not_found")
        remaining = args[4:]
        frequency = "1d"
        if remaining and remaining[0] in {"日线", "1分钟"}:
            frequency = "1m" if remaining.pop(0) == "1分钟" else "1d"
        adjustment = ""
        if remaining and remaining[0] in {"不复权", "前复权", "后复权"}:
            adjustment = {"不复权": "", "前复权": "qfq", "后复权": "hfq"}[remaining.pop(0)]
        fast, slow = 5, 20
        if remaining:
            if (
                strategy != "dual_ma"
                or len(remaining) != 2
                or not all(v.isascii() and v.isdigit() for v in remaining)
            ):
                raise ValueError("quant_backtest_invalid")
            fast, slow = map(int, remaining)
        if frequency == "1m" and adjustment:
            raise ValueError("quant_frequency_unsupported")
        market = infer_market(args[1])
        current, board = classify(market, args[1], local.date())
        previous = self.repository.find(service_id, command)
        if previous is not None:
            if previous.status != "completed":
                return f"回测 {str(previous.id)[:8]} 状态：{previous.status}。"
            return self._summary(
                previous.total_return,
                previous.max_drawdown,
                previous.execution_count,
                previous.data_sha256,
                args[0],
                current,
                start,
                end,
                fast,
                slow,
                settings,
                adjustment,
                frequency,
                previous.provider,
            )
        segments = [(current, start, end)]
        if market == "BJ":
            before = code_for_date(current, min(end, SWITCH_DATE - timedelta(days=1)))
            if start < SWITCH_DATE and before != current:
                segments = [(before, start, min(end, SWITCH_DATE - timedelta(days=1)))]
                if end >= SWITCH_DATE:
                    segments.append((current, SWITCH_DATE, end))
        days = self.source.trading_days(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        candidates = eligible_sources(
            settings.provider_order,
            market=market,
            frequency=frequency,
            adjustment=adjustment,
            configured=self.source.configured(),
        )

        def fetch(provider):
            bars = []
            for code, begin, finish in segments:
                if frequency == "1m":
                    ranges = (
                        {"start": begin.strftime("%Y%m%d"), "end": finish.strftime("%Y%m%d")}
                        if provider in {"tencent", "tushare", "gugudata"}
                        else {}
                    )
                    bars.extend(
                        bar
                        for bar in self.source.minutes(
                            market.lower(), code, provider=provider, **ranges
                        )
                        if begin <= bar.timestamp.astimezone(CHINA).date() <= finish
                    )
                elif provider == "sina":
                    bars.extend(
                        self.source.daily_sina(
                            market.lower(),
                            code,
                            begin.strftime("%Y%m%d"),
                            finish.strftime("%Y%m%d"),
                        )
                    )
                elif provider == "eastmoney":
                    bars.extend(
                        self.source.daily(
                            market.lower(),
                            code,
                            begin.strftime("%Y%m%d"),
                            finish.strftime("%Y%m%d"),
                            adjustment,
                        )
                    )
                else:
                    bars.extend(
                        self.source.daily_extra(
                            provider,
                            market.lower(),
                            code,
                            begin.strftime("%Y%m%d"),
                            finish.strftime("%Y%m%d"),
                            adjustment,
                        )
                    )
            if frequency == "1m":
                bars = normalize_auction_bars(bars)
            return validate_bars(bars, days, "daily" if frequency == "1d" else "1m")

        provider, bars = select_source(candidates, fetch)
        instrument = self.instruments.register(market, current, local.date())
        self.cache.store(instrument.id, provider, frequency, adjustment, bars, fetched_at=now)
        if not self._slots.acquire(blocking=False):
            raise ValueError("quant_backtest_busy")
        try:
            record, created = self.repository.begin(
                service_id,
                command,
                instrument.id,
                strategy,
                start,
                end,
                provider,
                adjustment,
                frequency=frequency,
            )
            if not created:
                if record.status != "completed":
                    return f"回测 {str(record.id)[:8]} 状态：{record.status}。"
                return self._summary(
                    record.total_return,
                    record.max_drawdown,
                    record.execution_count,
                    record.data_sha256,
                    args[0],
                    current,
                    start,
                    end,
                    fast,
                    slow,
                    settings,
                    adjustment,
                    frequency,
                    record.provider or provider,
                )
            try:
                result = self.native(
                    BacktestRequest(
                        current,
                        board,
                        strategy,
                        bars,
                        days,
                        settings.initial_cash,
                        settings.commission_rate,
                        settings.minimum_commission,
                        settings.stamp_tax_rate,
                        settings.slippage,
                        fast,
                        slow,
                        frequency,
                    )
                )
                self.repository.complete(record.id, result)
            except Exception:
                self.repository.fail(record.id, "quant_backtest_failed")
                raise
            return self._summary(
                result.total_return,
                result.max_drawdown,
                result.execution_count,
                result.data_sha256,
                args[0],
                current,
                start,
                end,
                fast,
                slow,
                settings,
                adjustment,
                frequency,
                provider,
            )
        finally:
            self._slots.release()

    @staticmethod
    def _summary(
        total,
        drawdown,
        executions,
        digest,
        strategy,
        code,
        start,
        end,
        fast,
        slow,
        settings,
        adjustment,
        frequency,
        provider,
    ) -> str:
        period = f"，均线 {fast}/{slow}" if strategy == "双均线" else ""
        frequency_label = "日线" if frequency == "1d" else "1分钟"
        return (
            f"{strategy} {code} {frequency_label} {start} 至 {end}{period}："
            f"收益 {total * 100:.2f}%，"
            f"最大回撤 {drawdown * 100:.2f}%，成交 {executions} 次。"
            f"来源 {PROVIDER_NAMES[provider]}，"
            f"复权 {adjustment or '不复权'}，数据 {digest[:12]}；"
            f"佣金 {settings.commission_rate}，最低佣金 {settings.minimum_commission}，"
            f"卖出税 {settings.stamp_tax_rate}，滑点 {settings.slippage}。"
            "每次买入按可用资金的75%估算，结果仅为模拟。"
            + ("不复权回测未计入分红送转，收益可能失真。" if not adjustment else "")
        )
