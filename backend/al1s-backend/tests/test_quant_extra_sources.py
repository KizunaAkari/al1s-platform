import pytest

from al1s.quant.extra_sources import parse_gugudata, parse_tencent, parse_tushare


def test_tencent_minute_normalizes_lots_and_rejects_other_symbol():
    payload = {"code": 0, "data": {"sh600000": {"m1": [["202610271000", "10", "10.1", "10.2", "9.9", "23", {}, "0"]]}}}
    rows = parse_tencent(payload, "sh600000", "1m", "")
    assert rows[0]["day"] == "2026-10-27 10:00:00" and rows[0]["volume"] == "2300"
    with pytest.raises(ValueError, match="quant_source_invalid"):
        parse_tencent(payload, "sz000001", "1m", "")


def test_tencent_adjustment_must_match_requested_basis():
    payload = {"code": 0, "data": {"sh600000": {"day": [["2026-10-27", "10", "11", "12", "9", "20"]]}}}
    with pytest.raises(ValueError, match="quant_source_invalid"):
        parse_tencent(payload, "sh600000", "1d", "qfq")
    assert parse_tencent(payload, "sh600000", "1d", "")[0]["volume"] == "2000"


def test_gugudata_checks_business_status_symbol_and_volume_units():
    row = {"Symbol": "600000", "TimeKey": "2026-10-27 10:00:00", "Open": 10,
           "High": 11, "Low": 9, "Close": 10, "TradingVolume": 25, "TradingAmount": 25000}
    payload = {"DataStatus": {"StatusCode": 100}, "Data": [row]}
    assert parse_gugudata(payload, "600000", "1m")[0]["volume"] == "2500"
    with pytest.raises(ValueError, match="quant_source_invalid"):
        parse_gugudata(payload, "000001", "1m")
    with pytest.raises(ValueError, match="quant_source_unavailable"):
        parse_gugudata({"DataStatus": {"StatusCode": 104}, "Data": []}, "600000", "1m")


def test_gugudata_conflicting_documented_volume_unit_is_inferred_only_when_consistent():
    row = {"Symbol": "600000", "TimeKey": "2026-10-27 10:00:00", "Open": 10,
           "High": 11, "Low": 9, "Close": 10, "TradingVolume": 2500, "TradingAmount": 25000}
    payload = {"DataStatus": {"StatusCode": 100}, "Data": [row]}
    assert parse_gugudata(payload, "600000", "1m")[0]["volume"] == "2500"
    payload["Data"][0]["TradingAmount"] = 125000
    with pytest.raises(ValueError, match="quant_source_units_unverified"):
        parse_gugudata(payload, "600000", "1m")


def test_tushare_checks_identity_and_daily_and_minute_volume_units():
    fields = ["ts_code", "time", "open", "high", "low", "close", "vol"]
    payload = {"code": 0, "data": {"fields": fields,
               "items": [["600000.SH", "2026-10-27 10:00:00", 10, 11, 9, 10, 2500]]}}
    assert parse_tushare(payload, "600000.SH", "1m")[0]["volume"] == "2500"
    with pytest.raises(ValueError, match="quant_source_invalid"):
        parse_tushare(payload, "000001.SZ", "1m")


def test_tushare_historical_minutes_use_documented_history_endpoint(monkeypatch):
    from types import SimpleNamespace
    from al1s.quant import source_worker
    calls = []
    payload = {"code": 0, "data": {"fields": ["ts_code", "trade_time", "open", "high", "low", "close", "vol"],
               "items": [["600000.SH", "2026-10-27 10:00:00", 10, 11, 9, 10, 2500]]}}
    monkeypatch.setenv("AL1S_QUANT_SOURCE_KEY", "fixture-token")
    monkeypatch.setattr(source_worker, "limited_request", lambda _session, _method, _url, **kwargs: (calls.append(kwargs) or SimpleNamespace(json=lambda: payload)))
    request = {"kind": "minute_tushare", "market": "sh", "code": "600000", "start": "20261026", "end": "20261027"}
    result = source_worker.fetch_extra(None, source_worker.validate_request(request))
    assert result[0]["volume"] == "2500"
    assert calls[0]["json"]["api_name"] == "stk_mins" and calls[0]["json"]["params"]["freq"] == "1min"
