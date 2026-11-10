from tests.test_freqtrade_service import command as ft_command
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.freqtrade.contracts import DEFAULT_COMMANDS, FreqtradeSettings
from al1s.freqtrade.futures_service import execute_futures, trade_futures
from al1s.freqtrade.service import FreqtradeService


def setup_trade(action="short", args=("SOL", "100", "50"), current=None):
    service = SimpleNamespace(repository=MagicMock(), manager=MagicMock())
    native = MagicMock()
    native.call.side_effect = [[current] if current else [], {"trade_id": 1, "leverage": 50}]
    command = ft_command(action, args)
    return service, native, command


def test_auto_tier_sends_margin_and_native_short_side_without_multiplying_the_budget(monkeypatch):
    service, native, command = setup_trade()
    monkeypatch.setattr("al1s.freqtrade.futures_service.leverage_limit", lambda *_: 100)
    result = trade_futures(
        service,
        native,
        command,
        uuid4(),
        ("SOL/USDT:USDT", Decimal("100"), 50),
        FreqtradeSettings(futures_enabled=True),
    )
    payload = native.call.call_args_list[-1].args[1]
    assert (
        payload["side"] == "short" and payload["leverage"] == 50 and payload["stakeamount"] == 100
    )
    assert "保证金预算 100 USDT" in result and "50倍" in result


def test_opposite_side_is_rejected_before_any_native_write():
    service, native, command = setup_trade(
        current={
            "pair": "SOL/USDT:USDT",
            "is_short": False,
            "has_open_orders": False,
            "stake_amount": 100,
            "leverage": 3,
        }
    )
    with pytest.raises(ValueError, match="先平仓"):
        trade_futures(
            service,
            native,
            command,
            uuid4(),
            ("SOL/USDT:USDT", Decimal("100"), 50),
            FreqtradeSettings(),
        )
    assert native.call.call_count == 1
    service.repository.prepare_native.assert_not_called()


def test_native_tier_limit_and_existing_leverage_are_enforced(monkeypatch):
    service, native, command = setup_trade()
    monkeypatch.setattr("al1s.freqtrade.futures_service.leverage_limit", lambda *_: 20)
    with pytest.raises(ValueError, match="1–20"):
        trade_futures(
            service,
            native,
            command,
            uuid4(),
            ("SOL/USDT:USDT", Decimal("100"), 50),
            FreqtradeSettings(),
        )
    assert native.call.call_count == 1
    service, native, command = setup_trade(
        action="long",
        args=("SOL", "100", "5"),
        current={
            "pair": "SOL/USDT:USDT",
            "is_short": False,
            "has_open_orders": False,
            "stake_amount": 100,
            "leverage": 3,
        },
    )
    with pytest.raises(ValueError, match="沿用当前"):
        trade_futures(
            service,
            native,
            command,
            uuid4(),
            ("SOL/USDT:USDT", Decimal("100"), 5),
            FreqtradeSettings(),
        )


def test_partial_close_only_uses_existing_trade_id_and_coin_quantity():
    service, native, command = setup_trade(
        action="close",
        args=("SOL", "0.5"),
        current={
            "trade_id": 4,
            "pair": "SOL/USDT:USDT",
            "is_short": True,
            "has_open_orders": False,
            "amount": 2,
        },
    )
    native.call.side_effect = [
        [{"trade_id": 4, "pair": "SOL/USDT:USDT", "amount": 2}],
        {"result": "Created exit order for trade 4."},
    ]
    trade_futures(
        service,
        native,
        command,
        uuid4(),
        ("SOL/USDT:USDT", Decimal("0.5"), None),
        FreqtradeSettings(),
    )
    assert native.call.call_args.args == (
        "forceexit",
        {"tradeid": "4", "ordertype": "market", "amount": 0.5},
    )


def test_unfinished_futures_write_blocks_new_entry_even_before_profile_recovery():
    repo, manager = MagicMock(), MagicMock()
    identity = uuid4()
    repo.profile.return_value = SimpleNamespace(id=uuid4(), service_id=identity, state="ready")
    repo.blocking_request.return_value = True
    command = SimpleNamespace(
        settings=FreqtradeSettings(futures_enabled=True),
        action="long",
        source=SimpleNamespace(sender_id="123"),
        arguments=("BTC", "100"),
        commands=DEFAULT_COMMANDS,
    )
    result = execute_futures(
        SimpleNamespace(repository=repo, manager=manager), identity, command, uuid4()
    )
    assert "待核实" in result
    manager.ensure.assert_not_called()


def test_unknown_old_trade_is_queried_by_id_without_crossing_market_wallets():
    repo, manager, native = MagicMock(), MagicMock(), MagicMock()
    request_id = uuid4()
    repo.unresolved.return_value = [
        SimpleNamespace(id=request_id, native_trade_id=1, native_order_count=1, action="close")
    ]
    native.call.return_value = {"trade_id": 1, "orders": [{}, {}], "pair": "BTC/USDT:USDT"}
    profile = SimpleNamespace(id=uuid4(), qq_id="123", market="futures")
    result = FreqtradeService(repo, manager).reconcile(profile, [], native)
    assert "恢复可操作" in result
    repo.unresolved.assert_called_once_with("123", market="futures")
    native.call.assert_called_once_with("trade/1")
    repo.finish_recovery.assert_called_once_with(
        profile.id, [request_id], all_recovered=True, market="futures"
    )
