import json
from types import SimpleNamespace

import pytest

from al1s.quant.source_client import MarketSource


def test_worker_receives_only_validated_request_and_no_bot_secrets():
    captured = []
    def run(args, **kwargs):
        captured.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [{"day": "2026-10-27 09:31:00",
            "open": "10", "high": "11", "low": "9", "close": "10", "volume": "10000"}]}).encode())
    source = MarketSource(run=run)
    bars = source.minutes("sh", "600000")
    assert bars[0].volume == 10000 and bars[0].timestamp.utcoffset().total_seconds() == 8 * 3600
    env = captured[0][1]["env"]
    assert "AL1S_MEME_DATABASE_URL" not in env and "HTTP_PROXY" not in env
    with pytest.raises(ValueError):
        source.minutes("sh", "../../etc/passwd")
    assert len(captured) == 1


def test_daily_volume_is_converted_from_lots_to_shares(monkeypatch):
    monkeypatch.setenv("AL1S_QUANT_EASTMONEY_PROXY", "http://proxy.example:7897")
    def run(_args, **_kwargs):
        row = {"日期": "2026-10-27", "开盘": "10", "最高": "11", "最低": "9",
               "收盘": "10", "成交量": "500"}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [row]}).encode())
    bars = MarketSource(run=run).daily("sh", "600000", "20261027", "20261027", "qfq")
    assert bars[0].volume == 50000


def test_calendar_returns_dates_without_filling_nontrading_days():
    from datetime import date
    def run(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [
            {"trade_date": "2026-10-08"}, {"trade_date": "2026-10-09"}]}).encode())
    days = MarketSource(run=run).trading_days("20261001", "20261009")
    assert days == {date(2026, 10, 8), date(2026, 10, 9)}


def test_corporate_rows_are_returned_without_guessing_missing_dates():
    def run(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [
            {"股权登记日": "2023-04-25", "股份到账日": "nan"}]}).encode())
    rows = MarketSource(run=run).corporate("sz", "300750")
    assert rows[0]["股份到账日"] == "nan"


def test_sina_daily_volume_remains_in_shares():
    def run(_args, **_kwargs):
        row = {"date": "2026-10-27", "open": "10", "high": "11", "low": "9",
               "close": "10", "volume": "50000"}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [row]}).encode())
    bars = MarketSource(run=run).daily_sina("sh", "600000", "20261027", "20261027")
    assert bars[0].volume == 50000


def test_eastmoney_minute_volume_is_converted_from_lots(monkeypatch):
    monkeypatch.setenv("AL1S_QUANT_EASTMONEY_PROXY", "http://proxy.example:7897")
    def run(_args, **_kwargs):
        row = {"时间": "2026-10-27 09:31:00", "开盘": "10", "最高": "11", "最低": "9",
               "收盘": "10", "成交量": "500", "成交额": "500000"}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [row]}).encode())
    bars = MarketSource(run=run).minutes("sh", "600000", provider="eastmoney")
    assert bars[0].volume == 50000


def test_only_eastmoney_worker_inherits_its_proxy(monkeypatch):
    captured = []
    monkeypatch.setenv("AL1S_QUANT_EASTMONEY_PROXY", "http://proxy.example:7897")

    def run(_args, **kwargs):
        captured.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=b'{"rows": []}')

    source = MarketSource(run=run)
    for request in (
        {"kind": "calendar", "start": "20261001", "end": "20261031"},
        {"kind": "corporate", "market": "sh", "code": "600000"},
        {"kind": "daily", "market": "sh", "code": "600000", "start": "20261001", "end": "20261031", "adjustment": ""},
        {"kind": "minute_east", "market": "sh", "code": "600000"},
    ):
        try:
            source._fetch_rows(request)
        except ValueError as error:
            assert str(error) == "quant_source_empty"
    assert all("AL1S_QUANT_EASTMONEY_PROXY" not in env for env in captured[:2])
    assert all(env["AL1S_QUANT_EASTMONEY_PROXY"] == "http://proxy.example:7897" for env in captured[2:])
    assert all("HTTPS_PROXY" not in env and "HTTP_PROXY" not in env for env in captured)


def test_eastmoney_does_not_fallback_to_direct_without_proxy(monkeypatch):
    monkeypatch.delenv("AL1S_QUANT_EASTMONEY_PROXY", raising=False)
    with pytest.raises(ValueError, match="quant_source_unavailable"):
        MarketSource().minutes("sh", "600000", provider="eastmoney")


def test_eastmoney_proxy_failure_does_not_retry_direct_or_use_another_source(monkeypatch):
    monkeypatch.setenv("AL1S_QUANT_EASTMONEY_PROXY", "http://proxy.example:7897")
    calls = []

    def run(_args, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=1, stdout=b'{"error": "ProxyError"}')

    with pytest.raises(ValueError, match="quant_source_unavailable"):
        MarketSource(run=run).minutes("sh", "600000", provider="eastmoney")
    assert calls == [{"kind": "minute_east", "market": "sh", "code": "600000"}]


def test_extra_provider_secret_is_only_passed_to_its_worker():
    calls = []

    def run(_args, **kwargs):
        calls.append(kwargs)
        row = {"day": "2026-10-27 10:00:00", "open": "10", "high": "11",
               "low": "9", "close": "10", "volume": "2500"}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [row]}).encode())

    source = MarketSource(run=run, credential=lambda provider: "private-key" if provider == "tushare" else None)
    assert source.minutes("sh", "600000", provider="tushare")[0].volume == 2500
    assert calls[0]["env"]["AL1S_QUANT_SOURCE_KEY"] == "private-key"
    assert b"private-key" not in calls[0]["input"]
    source.minutes("sh", "600000", provider="tencent")
    assert "AL1S_QUANT_SOURCE_KEY" not in calls[1]["env"]
    with pytest.raises(ValueError, match="quant_source_unavailable"):
        source.minutes("sh", "600000", provider="gugudata")


def test_batch_snapshots_credentials_once_and_releases_them_afterwards():
    snapshots, calls = [], []
    def snapshot():
        snapshots.append(1)
        return {"tushare": "private-key"}
    def run(_args, **kwargs):
        calls.append(kwargs["env"])
        row = {"day": "2026-10-27 10:00:00", "open": "10", "high": "11", "low": "9", "close": "10", "volume": "2500"}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [row]}).encode())
    source = MarketSource(run=run, credential_snapshot=snapshot)
    with source.batch_credentials():
        assert source.configured() == {"tushare"}
        source.minutes("sh", "600000", provider="tushare")
        source.minutes("sh", "600001", provider="tushare")
    assert len(snapshots) == 1 and len(calls) == 2
    assert source.configured() == set()


def test_provider_float_prices_are_normalized_to_storage_precision():
    def run(_args, **_kwargs):
        row = {"day": "2026-10-27 10:00:00", "open": "17.56999969482422", "high": "18",
               "low": "17", "close": "17.56999969482422", "volume": "2500"}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [row]}).encode())
    bar = MarketSource(run=run).minutes("sh", "600000", provider="tencent")[0]
    assert str(bar.close) == "17.570000"


def test_source_health_does_not_report_invalid_bar_as_success():
    responses = [
        {"day": "2026-10-27 10:00:00", "open": "10", "high": "11", "low": "9", "close": "10", "volume": "100"},
        {"day": "2026-10-27 10:01:00", "open": "invalid", "high": "11", "low": "9", "close": "10", "volume": "100"},
    ]
    def run(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": [responses.pop(0)]}).encode())
    source = MarketSource(run=run)
    source.minutes("sh", "600000", provider="tencent")
    before = source.health()["minute_tencent"]["last_success"]
    with pytest.raises(ValueError, match="quant_source_bar_invalid"):
        source.minutes("sh", "600000", provider="tencent")
    after = source.health()["minute_tencent"]
    assert after["error"] == "quant_source_bar_invalid" and after["last_success"] == before
