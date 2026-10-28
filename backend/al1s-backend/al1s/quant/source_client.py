"""Invoke guarded AKShare workers and normalize their returned bars."""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps
from threading import BoundedSemaphore, Lock, local

from al1s.quant.market_data import MarketBar
from al1s.quant.market_rules import CHINA
from al1s.quant.source_worker import MAX_RESPONSE, validate_request


def observed(kind):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            key = kind(*args, **kwargs) if callable(kind) else kind
            try:
                result = method(self, *args, **kwargs)
            except ValueError as error:
                with self._health_lock:
                    previous = self._health.get(key, {})
                    self._health[key] = {"last_success": previous.get("last_success"), "error": str(error),
                                         "checked_at": datetime.now(UTC).isoformat()}
                raise
            with self._health_lock:
                now = datetime.now(UTC).isoformat()
                self._health[key] = {"last_success": now, "error": None, "checked_at": now}
            return result
        return wrapped
    return decorate


class MarketSource:
    def __init__(
        self,
        *,
        run: Callable = subprocess.run,
        credential: Callable[[str], str | None] = lambda _: None,
        configured: Callable[[], set[str]] = set,
        credential_snapshot: Callable[[], dict[str, str]] | None = None,
    ):
        self._run = run
        self._credential = credential
        self._configured = configured
        self._credential_snapshot = credential_snapshot
        self._local = local()
        self._slots = BoundedSemaphore(2)
        self._health_lock = Lock()
        self._health: dict[str, dict[str, str | None]] = {}

    def _rows(self, request: dict) -> list[dict[str, str]]:
        return self._fetch_rows(request)

    def health(self) -> dict[str, dict[str, str | None]]:
        with self._health_lock:
            return {key: dict(value) for key, value in self._health.items()}

    def configured(self) -> set[str]:
        if hasattr(self._local, "credentials"):
            return set(self._local.credentials)
        return self._configured()

    @contextmanager
    def batch_credentials(self):
        previous = getattr(self._local, "credentials", None)
        if self._credential_snapshot is not None:
            self._local.credentials = self._credential_snapshot()
        try:
            yield
        finally:
            if previous is None:
                if hasattr(self._local, "credentials"):
                    del self._local.credentials
            else:
                self._local.credentials = previous

    def _fetch_rows(self, request: dict) -> list[dict[str, str]]:
        request = validate_request(request)
        env = {
            name: value
            for name in ("PATH", "PYTHONPATH", "SSL_CERT_FILE", "LANG", "TZ")
            if (value := os.environ.get(name)) is not None
        }
        if request["kind"] in {"minute_east", "daily"}:
            proxy = os.environ.get("AL1S_QUANT_EASTMONEY_PROXY")
            if not proxy:
                raise ValueError("quant_source_unavailable")
            env["AL1S_QUANT_EASTMONEY_PROXY"] = proxy
        provider = request["kind"].rsplit("_", 1)[-1]
        if provider in {"tushare", "gugudata"}:
            snapshot = getattr(self._local, "credentials", None)
            secret = snapshot.get(provider) if snapshot is not None else self._credential(provider)
            if not secret:
                raise ValueError("quant_source_unavailable")
            env["AL1S_QUANT_SOURCE_KEY"] = secret
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "POLARS_MAX_THREADS": "1",
                "RAYON_NUM_THREADS": "1",
            }
        )
        try:
            if not self._slots.acquire(timeout=3):
                raise ValueError("quant_source_busy")
            try:
                result = self._run(
                    [sys.executable, "-m", "al1s.quant.source_worker"],
                    input=json.dumps(request).encode(),
                    capture_output=True,
                    timeout=20,
                    env=env,
                    check=False,
                )
            finally:
                self._slots.release()
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("quant_source_unavailable") from error
        if result.returncode != 0 or len(result.stdout) > MAX_RESPONSE:
            raise ValueError("quant_source_unavailable")
        try:
            data = json.loads(result.stdout)
            rows = data["rows"]
            if (
                not isinstance(rows, list)
                or len(rows) > 8000
                or any(not isinstance(row, dict) for row in rows)
            ):
                raise ValueError("quant_source_invalid")
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError("quant_source_invalid") from error
        if not rows and (request["kind"].startswith(("minute", "daily"))):
            raise ValueError("quant_source_empty")
        return rows

    @staticmethod
    def _bar(row: dict[str, str], *, daily: bool) -> MarketBar:
        try:
            day = datetime.fromisoformat(row["日期"] if daily else row["day"])
            if day.tzinfo is not None:
                raise ValueError("unexpected_timezone")
            if daily:
                day = datetime.combine(day.date(), time(15))
            keys = (
                ("开盘", "最高", "最低", "收盘", "成交量")
                if daily
                else ("open", "high", "low", "close", "volume")
            )
            prices = [
                Decimal(str(row[key])).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                for key in keys[:4]
            ]
            volume = Decimal(str(row[keys[4]])) * (100 if daily else 1)
            if (
                any(not value.is_finite() or value <= 0 for value in prices)
                or not volume.is_finite()
                or volume < 0
                or volume != volume.to_integral_value()
                or prices[1] < max(prices[0], prices[3])
                or prices[2] > min(prices[0], prices[3])
                or prices[1] < prices[2]
            ):
                raise ValueError("invalid_values")
            return MarketBar(day.replace(tzinfo=CHINA), *prices, int(volume))
        except (KeyError, ValueError, TypeError, InvalidOperation) as error:
            raise ValueError("quant_source_bar_invalid") from error

    @observed(lambda *_args, provider="sina", **_kwargs: "minute" if provider == "sina" else "minute_east" if provider == "eastmoney" else f"minute_{provider}")
    def minutes(
        self,
        market: str,
        code: str,
        *,
        provider: str = "sina",
        start: str | None = None,
        end: str | None = None,
    ) -> list[MarketBar]:
        if provider == "sina":
            rows = self._rows({"kind": "minute", "market": market, "code": code})
            return self._unique([self._bar(row, daily=False) for row in rows])
        if provider in {"tencent", "tushare", "gugudata"}:
            request = {"kind": f"minute_{provider}", "market": market, "code": code}
            if start is not None or end is not None:
                request.update(start=start, end=end)
            rows = self._rows(request)
            return self._unique([self._bar(row, daily=False) for row in rows])
        if provider != "eastmoney":
            raise ValueError("quant_provider_invalid")
        rows = self._rows({"kind": "minute_east", "market": market, "code": code})
        bars, samples, consistent = [], 0, 0
        for row in rows:
            try:
                volume, amount, close = (
                    Decimal(str(row[key])) for key in ("成交量", "成交额", "收盘")
                )
                if volume > 0 and amount > 0 and close > 0:
                    samples += 1
                    if Decimal("80") <= amount / (volume * close) <= Decimal("120"):
                        consistent += 1
                adapted = {
                    "day": row["时间"],
                    "open": row["开盘"],
                    "high": row["最高"],
                    "low": row["最低"],
                    "close": row["收盘"],
                    "volume": str(volume * 100),
                }
                bars.append(self._bar(adapted, daily=False))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        if not samples or consistent * 100 < samples * 95 or not bars:
            raise ValueError("quant_source_units_unverified")
        return self._unique(bars)

    @observed("daily")
    def daily(
        self, market: str, code: str, start: str, end: str, adjustment: str
    ) -> list[MarketBar]:
        rows = self._rows(
            {
                "kind": "daily",
                "market": market,
                "code": code,
                "start": start,
                "end": end,
                "adjustment": adjustment,
            }
        )
        return self._unique([self._bar(row, daily=True) for row in rows])

    @observed("daily_sina")
    def daily_sina(self, market: str, code: str, start: str, end: str) -> list[MarketBar]:
        rows = self._rows(
            {
                "kind": "daily_sina",
                "market": market,
                "code": code,
                "start": start,
                "end": end,
                "adjustment": "",
            }
        )
        try:
            return self._unique(
                [self._bar({**row, "day": row["date"] + " 15:00:00"}, daily=False) for row in rows]
            )
        except KeyError as error:
            raise ValueError("quant_source_bar_invalid") from error

    @observed(lambda provider, *_args, **_kwargs: f"daily_{provider}")
    def daily_extra(
        self, provider: str, market: str, code: str, start: str, end: str, adjustment: str
    ) -> list[MarketBar]:
        if provider not in {"tencent", "tushare", "gugudata"}:
            raise ValueError("quant_provider_invalid")
        rows = self._rows(
            {
                "kind": f"daily_{provider}",
                "market": market,
                "code": code,
                "start": start,
                "end": end,
                "adjustment": adjustment,
            }
        )
        return self._unique([self._bar(row, daily=False) for row in rows])

    @observed("calendar")
    def trading_days(self, start: str, end: str) -> set[date]:
        rows = self._rows({"kind": "calendar", "start": start, "end": end})
        try:
            days = [date.fromisoformat(row["trade_date"]) for row in rows]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("quant_calendar_invalid") from error
        if len(set(days)) != len(days):
            raise ValueError("quant_calendar_invalid")
        return set(days)

    @observed("corporate")
    def corporate(self, market: str, code: str) -> list[dict[str, str]]:
        return self._rows({"kind": "corporate", "market": market, "code": code})

    @staticmethod
    def _unique(bars: list[MarketBar]) -> list[MarketBar]:
        if len({bar.timestamp for bar in bars}) != len(bars):
            raise ValueError("quant_source_duplicate_bar")
        return sorted(bars, key=lambda bar: bar.timestamp)
