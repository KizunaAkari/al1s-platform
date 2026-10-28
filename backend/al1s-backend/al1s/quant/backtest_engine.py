"""Two fixed AKQuant daily backtest strategies; no user-supplied code."""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_DOWN, Decimal
from importlib.metadata import version

from al1s.quant.market_data import MarketBar, validate_bars
from al1s.quant.market_rules import CHINA


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    symbol: str
    board: str
    strategy: str
    bars: list[MarketBar]
    trading_days: set[date]
    initial_cash: Decimal
    commission_rate: Decimal
    minimum_commission: Decimal
    stamp_tax_rate: Decimal
    slippage: Decimal
    fast: int = 5
    slow: int = 20
    frequency: str = "1d"


@dataclass(frozen=True, slots=True)
class BacktestResult:
    total_return: Decimal
    max_drawdown: Decimal
    execution_count: int
    end_equity: Decimal
    data_sha256: str


def validate_backtest(request: BacktestRequest) -> list[MarketBar]:
    if request.strategy not in {"buy_hold", "dual_ma"}:
        raise ValueError("quant_strategy_invalid")
    if (
        not re.fullmatch(r"[0-9]{6}", request.symbol)
        or request.board not in {"main", "star", "beijing"}
        or type(request.fast) is not int
        or type(request.slow) is not int
        or not 1 <= request.fast < request.slow <= 250
    ):
        raise ValueError("quant_backtest_invalid")
    for amount in (
        request.initial_cash,
        request.commission_rate,
        request.minimum_commission,
        request.stamp_tax_rate,
        request.slippage,
    ):
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError("quant_backtest_invalid")
    if request.initial_cash == 0 or any(
        rate > 1 for rate in (request.commission_rate, request.stamp_tax_rate, request.slippage)
    ):
        raise ValueError("quant_backtest_invalid")
    if request.frequency not in {"1d", "1m"}:
        raise ValueError("quant_frequency_unsupported")
    bars = validate_bars(
        request.bars, request.trading_days, "daily" if request.frequency == "1d" else "1m"
    )
    if request.strategy == "dual_ma" and len(bars) <= request.slow:
        raise ValueError("quant_market_data_missing")
    return bars


def _buy_quantity(cash: Decimal, price: Decimal, request: BacktestRequest) -> int:
    # Keep cash available for a price gap between the signal and the next bar.
    budget = cash * Decimal("0.75") - request.minimum_commission
    cost = price * (1 + request.slippage) * (1 + request.commission_rate)
    if budget <= 0 or cost <= 0:
        return 0
    quantity = int((budget / cost).to_integral_value(rounding=ROUND_DOWN))
    if request.board == "main":
        return quantity // 100 * 100
    return quantity if quantity >= (200 if request.board == "star" else 100) else 0


def run_backtest(request: BacktestRequest) -> BacktestResult:
    bars = validate_backtest(request)
    if version("akquant") != "0.3.71":
        raise ValueError("quant_engine_version_unsupported")
    import akquant
    import pandas as pd

    prices: list[Decimal] = []
    state = {"order_pending": False, "buy_hold_sent": False}

    def on_bar(ctx, bar):
        price = Decimal(str(bar.close))
        prices.append(price)
        if request.frequency == "1m":
            local = datetime.fromtimestamp(int(bar.timestamp) // 1_000_000_000, UTC).astimezone(
                CHINA
            )
            if local.time().replace(tzinfo=None) >= time(14, 57):
                return
        if state["order_pending"]:
            return
        position = Decimal(str(ctx.get_position(request.symbol)))
        buy = request.strategy == "buy_hold" and not state["buy_hold_sent"]
        sell = False
        if request.strategy == "dual_ma" and len(prices) >= request.slow:
            fast = sum(prices[-request.fast :]) / request.fast
            slow = sum(prices[-request.slow :]) / request.slow
            buy, sell = fast > slow and position == 0, fast < slow and position > 0
        if buy:
            state["buy_hold_sent"] = True
            quantity = _buy_quantity(Decimal(str(ctx.get_account()["cash"])), price, request)
            if quantity:
                state["order_pending"] = True
                ctx.buy(symbol=request.symbol, quantity=quantity)
        elif sell:
            available = int(ctx.get_available_position(request.symbol))
            if available > 0:
                state["order_pending"] = True
                ctx.sell(symbol=request.symbol, quantity=available)

    def on_order(_ctx, order):
        if str(order.status).rsplit(".", 1)[-1].lower() in {
            "filled",
            "rejected",
            "cancelled",
            "expired",
        }:
            state["order_pending"] = False

    frame = pd.DataFrame(
        [
            {
                "date": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": bar.volume,
            }
            for bar in bars
        ]
    )
    result = akquant.run_backtest(
        data=frame,
        strategy=on_bar,
        on_order=on_order,
        initial_cash=float(request.initial_cash),
        symbols=request.symbol,
        show_progress=False,
        commission_rate=float(request.commission_rate),
        min_commission=float(request.minimum_commission),
        stamp_tax_rate=float(request.stamp_tax_rate),
        transfer_fee_rate=0,
        slippage={"type": "percent", "value": float(request.slippage)},
        t_plus_one=True,
        lot_size=100 if request.board == "main" else 1,
    )
    equity = [Decimal(str(value)) for value in result.equity_curve]
    if not equity or any(not value.is_finite() or value < 0 for value in equity):
        raise ValueError("quant_backtest_result_invalid")
    peak, drawdown = request.initial_cash, Decimal(0)
    for value in equity:
        peak = max(peak, value)
        if peak:
            drawdown = max(drawdown, (peak - value) / peak)
    identity = "\n".join(
        f"{bar.timestamp.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}"
        for bar in bars
    ).encode()
    return BacktestResult(
        (equity[-1] - request.initial_cash) / request.initial_cash,
        drawdown,
        len(result.executions_df),
        equity[-1],
        hashlib.sha256(identity).hexdigest(),
    )
