"""Normalize additional source responses without guessing missing data."""

from datetime import datetime
from decimal import Decimal, InvalidOperation


def _bar(day, opening, high, low, close, volume, factor):
    try:
        text = str(day)
        if len(text) == 12 and text.isdigit():
            timestamp = datetime.strptime(text, "%Y%m%d%H%M")
        elif len(text) == 8 and text.isdigit():
            timestamp = datetime.strptime(text, "%Y%m%d").replace(hour=15)
        elif len(text) == 10:
            timestamp = datetime.fromisoformat(text).replace(hour=15)
        else:
            timestamp = datetime.fromisoformat(text)
        quantity = Decimal(str(volume)) * factor
        if (
            timestamp.tzinfo is not None
            or not quantity.is_finite()
            or quantity < 0
            or quantity != quantity.to_integral_value()
        ):
            raise ValueError
        return {
            "day": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": str(opening),
            "high": str(high),
            "low": str(low),
            "close": str(close),
            "volume": str(int(quantity)),
        }
    except (ValueError, TypeError, InvalidOperation) as error:
        raise ValueError("quant_source_invalid") from error


def parse_tencent(payload: dict, symbol: str, frequency: str, adjustment: str) -> list[dict]:
    try:
        if payload["code"] != 0:
            raise ValueError("quant_source_unavailable")
        key = "m1" if frequency == "1m" else f"{adjustment}day"
        rows = payload["data"][symbol][key]
        if not isinstance(rows, list):
            raise ValueError("quant_source_invalid")
        factor = 1 if symbol.startswith("sh688") else 100
        return [_bar(row[0], row[1], row[3], row[4], row[2], row[5], factor) for row in rows]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("quant_source_invalid") from error


def parse_gugudata(payload: dict, code: str, frequency: str) -> list[dict]:
    try:
        if payload["DataStatus"]["StatusCode"] != 100:
            raise ValueError("quant_source_unavailable")
        rows = payload["Data"]
        if not isinstance(rows, list) or any(str(row["Symbol"]).zfill(6) != code for row in rows):
            raise ValueError("quant_source_invalid")
        shares, lots, samples = 0, 0, 0
        for row in rows:
            try:
                volume = Decimal(str(row["TradingVolume"]))
                amount = Decimal(str(row["TradingAmount"]))
                close = Decimal(str(row["Close"]))
                if not all(value.is_finite() for value in (volume, amount, close)):
                    raise ValueError("quant_source_units_unverified")
                if volume > 0 and amount > 0 and close > 0:
                    samples += 1
                    ratio = amount / (volume * close)
                    shares += Decimal("0.8") <= ratio <= Decimal("1.2")
                    lots += Decimal("80") <= ratio <= Decimal("120")
            except (KeyError, InvalidOperation, TypeError) as error:
                raise ValueError("quant_source_units_unverified") from error
        if not samples or max(shares, lots) * 100 < samples * 95:
            raise ValueError("quant_source_units_unverified")
        factor = 1 if shares > lots else 100
        return [
            _bar(
                row["TimeKey"],
                row["Open"],
                row["High"],
                row["Low"],
                row["Close"],
                row["TradingVolume"],
                factor,
            )
            for row in rows
        ]
    except (KeyError, TypeError) as error:
        raise ValueError("quant_source_invalid") from error


def parse_tushare(payload: dict, symbol: str, frequency: str) -> list[dict]:
    try:
        if payload["code"] != 0:
            raise ValueError("quant_source_unavailable")
        fields, items = payload["data"]["fields"], payload["data"]["items"]
        if (
            not isinstance(fields, list)
            or len(set(fields)) != len(fields)
            or not isinstance(items, list)
        ):
            raise ValueError("quant_source_invalid")
        rows = [dict(zip(fields, item, strict=True)) for item in items]
        if any(row.get("ts_code", row.get("code")) != symbol for row in rows):
            raise ValueError("quant_source_invalid")
        factor = 1 if frequency == "1m" else 100
        key = ("time" if "time" in fields else "trade_time") if frequency == "1m" else "trade_date"
        return [
            _bar(row[key], row["open"], row["high"], row["low"], row["close"], row["vol"], factor)
            for row in rows
        ]
    except (KeyError, TypeError) as error:
        raise ValueError("quant_source_invalid") from error
