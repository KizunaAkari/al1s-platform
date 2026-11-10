from tests.test_freqtrade_service import command as ft_command
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.freqtrade.capital_service import execute_capital, parse_capital, receipt_matches
from al1s.freqtrade.commands import validate_words
from al1s.freqtrade.contracts import DEFAULT_COMMANDS


def test_capital_amount_and_command_contract():
    assert len(DEFAULT_COMMANDS) == 19
    assert parse_capital("f_margin", ("BTC", "100")) == ("BTC/USDT:USDT", Decimal("100"))
    assert parse_capital("deposit", ("1000.0001",)) == (None, Decimal("1000.0001"))
    validate_words(DEFAULT_COMMANDS, set())
    validate_words(
        {
            k: v
            for k, v in DEFAULT_COMMANDS.items()
            if k not in {"deposit", "f_deposit", "f_margin"}
        },
        set(),
    )
    for value in ("0", "-1", "NaN", "1.00001", "1000000001"):
        with pytest.raises(ValueError):
            parse_capital("f_deposit", (value,))


def test_margin_request_is_not_a_buy_and_checks_owned_position():
    request = uuid4()
    service, native = SimpleNamespace(repository=MagicMock()), MagicMock()
    native.call.side_effect = [
        [dict(trade_id=4, pair="BTC/USDT:USDT", amount=1, stake_amount=100, has_open_orders=False)],
        dict(
            operation_id=str(request),
            action="margin",
            amount="50.0000",
            trade_id=4,
            free_after=10,
            added_margin="50",
            total_collateral="150",
            liquidation_price=70000,
        ),
    ]
    command = ft_command("f_margin", ("BTC", "50"))
    result = execute_capital(service, native, command, request)
    service.repository.prepare_capital.assert_called_once_with(request, Decimal("50"), 4)
    payload = native.call.call_args.args[1]
    assert native.call.call_args.args[0] == "al1s/capital"
    assert payload["action"] == "margin" and payload["trade_id"] == 4
    assert "币数量不变" in result


def test_margin_with_pending_order_is_rejected_before_persist_or_native_write():
    service, native = SimpleNamespace(repository=MagicMock()), MagicMock()
    native.call.return_value = [dict(trade_id=4, pair="BTC/USDT:USDT", has_open_orders=True)]
    with pytest.raises(ValueError, match="在途"):
        execute_capital(
            service, native, SimpleNamespace(action="f_margin", arguments=("BTC", "50")), uuid4()
        )
    service.repository.prepare_capital.assert_not_called()
    assert native.call.call_count == 1


def test_unknown_capital_is_verified_by_operation_amount_and_trade():
    identity = uuid4()
    pending = SimpleNamespace(
        id=identity, action="f_margin", capital_amount=Decimal("100"), native_trade_id=4
    )
    evidence = dict(operation_id=str(identity), action="margin", amount="100.0000", trade_id=4)
    assert receipt_matches(pending, evidence)
    assert not receipt_matches(pending, {**evidence, "amount": "200"})
    assert not receipt_matches(pending, {**evidence, "trade_id": 5})
    assert not receipt_matches(pending, {**evidence, "operation_id": str(uuid4())})


def test_margin_status_read_failure_is_explicitly_not_submitted():
    service,native=SimpleNamespace(repository=MagicMock()),MagicMock()
    native.call.side_effect=OSError('read unavailable')
    with pytest.raises(ValueError,match='未发送'):
        execute_capital(service,native,
            SimpleNamespace(action='f_margin',arguments=('BTC','100')),uuid4())
    service.repository.prepare_capital.assert_not_called()
