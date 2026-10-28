"""Deterministic paper-ledger arithmetic, checked against the engine result."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

MONEY_UNIT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class Lot:
    acquired_on: date
    available_on: date
    quantity: int
    cost: Decimal
    id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    side: str
    quantity: int
    price: Decimal
    fees: Decimal
    trade_date: date


@dataclass(frozen=True, slots=True)
class BookUpdate:
    cash: Decimal
    lots: tuple[Lot, ...]


def _money(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("quant_amount_invalid")
    rounded = value.quantize(MONEY_UNIT, rounding=ROUND_HALF_UP)
    if rounded.copy_abs() >= Decimal("10000000000000000"):
        raise ValueError("quant_amount_invalid")
    return rounded


def apply_fill(
    cash: Decimal,
    lots: list[Lot] | tuple[Lot, ...],
    fill: Fill,
    *,
    next_trading_day: date,
    engine_cash: Decimal,
    engine_position: int,
) -> BookUpdate:
    if (
        fill.side not in {"buy", "sell"}
        or type(fill.quantity) is not int
        or fill.quantity <= 0
        or not isinstance(fill.price, Decimal)
        or not fill.price.is_finite()
        or fill.price <= 0
        or not isinstance(fill.fees, Decimal)
        or not fill.fees.is_finite()
        or fill.fees < 0
        or next_trading_day <= fill.trade_date
        or type(engine_position) is not int
    ):
        raise ValueError("quant_fill_invalid")
    if fill.price != fill.price.quantize(Decimal("0.000001")) or fill.fees != fill.fees.quantize(
        MONEY_UNIT
    ):
        raise ValueError("quant_fill_precision_invalid")
    cash = _money(cash)
    if cash < 0 or any(
        type(lot.quantity) is not int
        or lot.quantity <= 0
        or lot.cost < 0
        or lot.available_on < lot.acquired_on
        for lot in lots
    ):
        raise ValueError("quant_ledger_invalid")
    current = sorted(lots, key=lambda lot: (lot.acquired_on, lot.available_on))
    previous_position = sum(lot.quantity for lot in current)
    amount, fees = _money(fill.price * fill.quantity), _money(fill.fees)
    if fill.side == "buy":
        cash = _money(cash - amount - fees)
        if cash < 0:
            raise ValueError("quant_insufficient_cash")
        current.append(Lot(fill.trade_date, next_trading_day, fill.quantity, _money(amount + fees)))
        expected_position = previous_position + fill.quantity
    else:
        if (
            sum(lot.quantity for lot in current if lot.available_on <= fill.trade_date)
            < fill.quantity
        ):
            raise ValueError("quant_insufficient_available")
        remaining, reduced = fill.quantity, []
        for lot in current:
            sold = min(remaining, lot.quantity) if lot.available_on <= fill.trade_date else 0
            remaining -= sold
            if sold < lot.quantity:
                allocated = _money(lot.cost * sold / lot.quantity) if sold else Decimal(0)
                reduced.append(
                    Lot(
                        lot.acquired_on,
                        lot.available_on,
                        lot.quantity - sold,
                        _money(lot.cost - allocated),
                        lot.id,
                    )
                )
        current = reduced
        cash = _money(cash + amount - fees)
        if cash < 0:
            raise ValueError("quant_insufficient_cash")
        expected_position = previous_position - fill.quantity
    if _money(engine_cash) != cash or engine_position != expected_position:
        raise ValueError("quant_engine_mismatch")
    return BookUpdate(cash, tuple(current))
