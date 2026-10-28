"""AKQuant paper adapter for actual bars collected after a QQ request."""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as clock
from decimal import ROUND_HALF_UP, Decimal
from importlib.metadata import version
from uuid import uuid4

from al1s.quant.market_data import MarketBar
from al1s.quant.market_rules import CHINA


@dataclass(frozen=True, slots=True)
class PaperInstrument:
    symbol: str
    board: str


@dataclass(frozen=True, slots=True)
class PaperRequest:
    symbol: str
    side: str
    quantity: int
    requested_at: datetime
    instruments: tuple[PaperInstrument, ...]
    cash: Decimal
    state: bytes | None
    commission_rate: Decimal
    minimum_commission: Decimal
    stamp_tax_rate: Decimal
    slippage: Decimal
    expected_positions: tuple[tuple[str, int], ...]
    initial_cash: Decimal


@dataclass(frozen=True, slots=True)
class PaperResult:
    price: Decimal
    fees: Decimal
    quantity: int
    occurred_at: datetime
    cash: Decimal
    positions: dict[str, int]
    state: bytes


@dataclass(frozen=True, slots=True)
class EntitlementState:
    state: bytes
    cash: Decimal
    position: int
    available: int


def apply_paper_entitlement(
    state: bytes, symbol: str, shares: int, cash: Decimal, occurred_at: datetime
) -> EntitlementState:
    if (
        not isinstance(state, bytes)
        or not state
        or not re.fullmatch(r"[0-9]{6}", symbol)
        or type(shares) is not int
        or shares < 0
        or not isinstance(cash, Decimal)
        or not cash.is_finite()
        or cash < 0
        or occurred_at.tzinfo is None
        or version("akquant") != "0.3.71"
    ):
        raise ValueError("quant_entitlement_invalid")
    from akquant import Engine

    engine = Engine()
    engine.load_state_bytes(state)
    if not hasattr(engine, "apply_paper_entitlement"):
        raise ValueError("quant_entitlement_engine_unsupported")
    engine.apply_paper_entitlement(
        symbol, shares, str(cash), int(occurred_at.timestamp()) * 1_000_000_000
    )
    portfolio = engine.portfolio
    position = Decimal(str(portfolio.get_position(symbol)))
    available = Decimal(str(portfolio.get_available_position(symbol)))
    if position != position.to_integral_value() or available != available.to_integral_value():
        raise ValueError("quant_engine_mismatch")
    return EntitlementState(
        engine.get_state_bytes(),
        Decimal(str(portfolio.cash)).quantize(Decimal("0.0001")),
        int(position),
        int(available),
    )


def validate_paper_request(request: PaperRequest, bars: list[MarketBar]) -> list[MarketBar]:
    if (
        request.side not in {"buy", "sell"}
        or type(request.quantity) is not int
        or request.quantity <= 0
        or request.requested_at.tzinfo is None
        or not request.instruments
        or len(request.instruments) > 1000
        or len({item.symbol for item in request.instruments}) != len(request.instruments)
        or request.symbol not in {item.symbol for item in request.instruments}
        or any(
            item.board not in {"main", "star", "beijing"}
            or not re.fullmatch(r"[0-9]{6}", item.symbol)
            for item in request.instruments
        )
        or len(dict(request.expected_positions)) != len(request.expected_positions)
        or set(dict(request.expected_positions)) != {item.symbol for item in request.instruments}
        or any(type(qty) is not int or qty < 0 for _, qty in request.expected_positions)
    ):
        raise ValueError("quant_paper_request_invalid")
    for amount in (
        request.cash,
        request.initial_cash,
        request.commission_rate,
        request.minimum_commission,
        request.stamp_tax_rate,
        request.slippage,
    ):
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError("quant_paper_request_invalid")
    if request.initial_cash <= 0:
        raise ValueError("quant_paper_request_invalid")
    return validate_paper_bars(request.requested_at, bars)


def validate_paper_bars(requested_at: datetime, bars: list[MarketBar]) -> list[MarketBar]:
    if requested_at.tzinfo is None or len(bars) < 3 or len(bars) > 30:
        raise ValueError("quant_paper_bars_missing")
    last = requested_at
    trading_day = last.astimezone(CHINA).date()
    for bar in bars:
        if (
            bar.timestamp.tzinfo is None
            or bar.timestamp <= last
            or bar.timestamp.astimezone(CHINA).date() != trading_day
            or not (
                clock(9, 31)
                <= bar.timestamp.astimezone(CHINA).time().replace(tzinfo=None)
                <= clock(11, 30)
                or clock(13, 1)
                <= bar.timestamp.astimezone(CHINA).time().replace(tzinfo=None)
                <= clock(15)
            )
            or type(bar.volume) is not int
            or bar.volume <= 0
            or any(
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
                for value in (bar.open, bar.high, bar.low, bar.close)
            )
            or bar.high < max(bar.open, bar.close)
            or bar.low > min(bar.open, bar.close)
            or bar.high < bar.low
        ):
            raise ValueError("quant_paper_bar_invalid")
        last = bar.timestamp
    return bars


def _paced_replay(**kwargs):
    from akquant.gateway.brokers.replay.gateway import build_replay_bundle

    bundle = build_replay_bundle(**kwargs)
    gateway = bundle.market_gateway

    def start():
        for item in gateway.pending_events:
            gateway._feed.add_bar(item)
            time.sleep(0.05)

    gateway.start = start
    return bundle


def run_paper(request: PaperRequest, bars: list[MarketBar]) -> PaperResult:
    validate_paper_request(request, bars)
    if version("akquant") != "0.3.71":
        raise ValueError("quant_engine_version_unsupported")
    from akquant import AssetType, Instrument
    from akquant.akquant import Bar
    from akquant.gateway import register_broker
    from akquant.live._runner import LiveRunner
    from akquant.signal import QueueSignalSource, Signal

    class SlippageLiveRunner(LiveRunner):
        def _build_gateway_kwargs(self):
            kwargs = super()._build_gateway_kwargs()
            # LiveRunner replaces the simulated execution client inside run().
            self.engine.set_slippage("percent", float(request.slippage))
            self.engine.set_initial_cash_reference(float(request.initial_cash))
            return kwargs

    register_broker("al1s_qq_paper_v1", _paced_replay)
    instruments = [
        Instrument(
            symbol=item.symbol,
            asset_type=AssetType.Stock,
            multiplier=1,
            margin_ratio=1,
            tick_size=0.01,
            lot_size=100 if item.board == "main" else 1,
            option_type=None,
            strike_price=None,
            expiry_date=None,
        )
        for item in request.instruments
    ]
    feed = [
        Bar(
            timestamp=int(bar.timestamp.timestamp()) * 1_000_000_000,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
            symbol=request.symbol,
        )
        for bar in bars
    ]
    signal = QueueSignalSource()
    observed: dict = {"bars": 0, "trades": [], "positions": {}, "account": None, "errors": []}

    def on_bar(ctx, _bar):
        observed["bars"] += 1
        if observed["bars"] == 1:
            before = {
                item.symbol: Decimal(str(ctx.get_position(item.symbol)))
                for item in request.instruments
            }
            if (
                before != dict(request.expected_positions)
                or Decimal(str(ctx.get_account()["cash"])) != request.cash
            ):
                observed["errors"].append("state_mismatch")
                return
            signal.put(
                Signal(
                    signal_id=uuid4().hex,
                    symbol=request.symbol,
                    action=request.side,
                    quantity=request.quantity,
                )
            )
        observed["account"] = ctx.get_account()
        observed["positions"] = {
            item.symbol: Decimal(str(ctx.get_position(item.symbol))) for item in request.instruments
        }

    def on_trade(_ctx, trade):
        observed["trades"].append(trade)

    def on_error(_ctx, error, _stage, _data):
        observed["errors"].append(type(error).__name__)

    runner = SlippageLiveRunner(
        strategy_cls=on_bar,
        on_trade=on_trade,
        on_error=on_error,
        instruments=instruments,
        broker="al1s_qq_paper_v1",
        trading_mode="paper",
        gateway_options={"bars": feed},
        signal_source=signal,
    )
    if request.state is not None:
        runner.engine.load_state_bytes(request.state)
    for item in request.instruments:
        runner.engine.set_instrument_fee_override(
            item.symbol,
            commission_rate=float(request.commission_rate),
            min_commission=float(request.minimum_commission),
            stamp_tax_rate=float(request.stamp_tax_rate),
            transfer_fee_rate=0,
        )
    runner.run(cash=float(request.cash), show_progress=False, duration="10s")
    if observed["errors"] or len(observed["trades"]) != 1 or observed["account"] is None:
        raise ValueError("quant_paper_result_unknown")
    trade = observed["trades"][0]
    qty = Decimal(str(trade.quantity))
    if trade.symbol != request.symbol or qty != request.quantity:
        raise ValueError("quant_paper_result_unknown")
    positions = observed["positions"]
    if any(value != value.to_integral_value() for value in positions.values()):
        raise ValueError("quant_paper_result_unknown")
    expected = dict(request.expected_positions)
    expected[request.symbol] += request.quantity if request.side == "buy" else -request.quantity
    if positions != expected:
        raise ValueError("quant_paper_result_unknown")
    timestamp = int(trade.timestamp)
    occurred = datetime.fromtimestamp(timestamp // 1_000_000_000, UTC)
    if occurred <= request.requested_at or occurred not in {bar.timestamp for bar in bars}:
        raise ValueError("quant_paper_result_unknown")
    return PaperResult(
        Decimal(str(trade.price)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
        Decimal(str(trade.commission)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        int(qty),
        occurred,
        Decimal(str(observed["account"]["cash"])).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        ),
        {symbol: int(qty) for symbol, qty in positions.items()},
        runner.engine.get_state_bytes(),
    )
