from types import SimpleNamespace

import pytest

from al1s.quant.source_worker import configure_proxy, install_sina_guard, validate_request


def test_proxy_is_applied_only_to_eastmoney_requests():
    for kind in ("minute_east", "daily", "minute", "daily_sina", "calendar", "corporate"):
        session = SimpleNamespace(proxies={})
        configure_proxy(session, kind, "http://proxy.example:7897")
        expected = {"https": "http://proxy.example:7897"} if kind in {"minute_east", "daily"} else {}
        assert session.proxies == expected


def test_sina_daily_eval_path_is_blocked_even_for_unadjusted_minutes():
    module = SimpleNamespace(stock_zh_a_daily=lambda **_kwargs: "unsafe")
    install_sina_guard(module)
    with pytest.raises(ValueError, match="unsafe_adjustment_blocked"):
        module.stock_zh_a_daily(symbol="sh600000", adjust="qfq")


def test_only_fixed_provider_actions_and_stock_codes_are_accepted():
    assert validate_request({"kind": "minute", "market": "sh", "code": "600000"})["kind"] == "minute"
    assert validate_request({"kind": "minute", "market": "bj", "code": "920047"})["market"] == "bj"
    with pytest.raises(ValueError):
        validate_request({"kind": "minute", "market": "sh", "code": "../../etc/passwd"})
    with pytest.raises(ValueError):
        validate_request({"kind": "minute", "market": "sh", "code": "600000", "url": "https://example.com"})
    assert validate_request({"kind": "calendar", "start": "20261001", "end": "20261031"})["kind"] == "calendar"
    assert validate_request({"kind": "corporate", "market": "sz", "code": "300750"})["kind"] == "corporate"
    assert validate_request({"kind": "minute_east", "market": "sh", "code": "600000"})["kind"] == "minute_east"
    assert validate_request({"kind": "daily_sina", "market": "sh", "code": "600000",
                             "start": "20260101", "end": "20261027", "adjustment": ""})["kind"] == "daily_sina"
    assert validate_request({"kind": "minute_tencent", "market": "sh", "code": "600000"})["kind"] == "minute_tencent"
    with pytest.raises(ValueError):
        validate_request({"kind": "minute_tushare", "market": "sh", "code": "600000", "url": "https://example.com"})
    with pytest.raises(ValueError):
        validate_request({"kind": "daily_sina", "market": "sh", "code": "600000",
                          "start": "20260101", "end": "20261027", "adjustment": "qfq"})
