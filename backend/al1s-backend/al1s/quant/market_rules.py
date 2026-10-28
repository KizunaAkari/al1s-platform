from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")


def check_order_quantity(board: str, side: str, quantity: int, available: int) -> int:
    if (
        type(quantity) is not int
        or type(available) is not int
        or quantity <= 0
        or available < 0
        or side not in {"buy", "sell"}
        or board not in {"main", "star", "beijing"}
    ):
        raise ValueError("quant_quantity_invalid")
    if side == "sell" and quantity > available:
        raise ValueError("quant_quantity_exceeds_available")
    if board == "main":
        valid = quantity % 100 == 0 or (
            side == "sell" and quantity % 100 == available % 100 and available % 100 != 0
        )
    else:
        minimum = 200 if board == "star" else 100
        valid = quantity >= minimum or (side == "sell" and quantity == available)
    if not valid:
        raise ValueError("quant_quantity_invalid")
    return quantity


def check_quote(
    price: Decimal,
    quote_time: datetime,
    now: datetime,
    trading_days: set[date],
    max_age_seconds: int,
) -> Decimal:
    if not price.is_finite() or price <= 0 or quote_time.tzinfo is None or now.tzinfo is None:
        raise ValueError("quant_quote_invalid")
    local = now.astimezone(CHINA)
    clock = local.time().replace(tzinfo=None)
    if local.date() not in trading_days or not (
        time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(14, 57)
    ):
        raise ValueError("quant_market_session_closed")
    age = (now - quote_time).total_seconds()
    if age < 0 or age > max_age_seconds or quote_time.astimezone(CHINA).date() != local.date():
        raise ValueError("quant_quote_stale")
    return price
