from unittest.mock import MagicMock

import pytest

from al1s.quant.source_client import MarketSource
from al1s.quant.source_worker import fetch_name, fetch_names, validate_request


def test_name_uses_existing_fixed_endpoint_and_checks_returned_code(monkeypatch):
    response = MagicMock()
    response.json.return_value = {
        "data": {"sz300750": {"qt": {"sz300750": ["51", "宁德时代", "300750"]}}}
    }
    get = MagicMock(return_value=response)
    monkeypatch.setattr("al1s.quant.source_worker.limited_request", get)
    request = validate_request({"kind": "name", "market": "sz", "code": "300750"})
    assert fetch_name(MagicMock(), request) == [{"name": "宁德时代"}]
    assert get.call_args.args[2] == "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    response.json.return_value = {
        "data": {"sz300750": {"qt": {"sz300750": ["51", "其他股票", "600000"]}}}
    }
    assert fetch_name(MagicMock(), request) == []


def test_name_failure_is_optional_and_not_a_quote_failure():
    source = MarketSource()
    source._rows = MagicMock(side_effect=ValueError("unavailable"))
    assert source.security_name("sz", "300750") is None
    source._rows = MagicMock(return_value=[{"name": "宁德时代"}])
    assert source.security_name("sz", "300750") == "宁德时代"


def test_name_batch_is_bounded_and_checks_each_symbol():
    symbols = [{"market": "sz", "code": "300750"}, {"market": "sh", "code": "600000"}]
    assert validate_request({"kind": "name", "symbols": symbols})["symbols"] == symbols
    with pytest.raises(ValueError):
        validate_request({"kind": "name", "symbols": symbols * 13})
    with pytest.raises(ValueError):
        validate_request({"kind": "name", "symbols": [symbols[0], symbols[0]]})
    with pytest.raises(ValueError):
        validate_request({"kind": "name", "symbols": [{"market": "sh", "code": "300750"}]})


def test_batch_uses_existing_code_checked_lookup_and_keeps_partial_results(monkeypatch):
    def lookup(_session, symbol):
        if symbol['code'] == '600000':
            raise OSError('source unavailable')
        return [{'name': '宁德时代'}]
    monkeypatch.setattr('al1s.quant.source_worker.fetch_name', lookup)
    session = MagicMock()
    session.__enter__.return_value = session
    rows = fetch_names([{'market': 'sz', 'code': '300750'}, {'market': 'sh', 'code': '600000'}], lambda: session)
    assert rows == [{'market': 'sz', 'code': '300750', 'name': '宁德时代'}]


def test_batch_client_accepts_only_requested_codes_and_valid_names():
    source = MarketSource()
    source._rows = MagicMock(return_value=[
        {'market': 'sz', 'code': '300750', 'name': '宁德时代'},
        {'market': 'sh', 'code': '600000', 'name': '600000'},
        {'market': 'sh', 'code': '600001', 'name': '其他'},
    ])
    assert source.security_names([('SZ', '300750'), ('SH', '600000'), ('SZ', '300750')]) == {
        ('SZ', '300750'): '宁德时代'
    }
    assert len(source._rows.call_args.args[0]['symbols']) == 2
