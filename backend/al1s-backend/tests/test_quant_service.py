from decimal import Decimal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.postgres.quant_accounts import Account, AccountSummary
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.commands import DEFAULT_COMMANDS, QuantCommand
from al1s.quant.contracts import QuantSettings
from al1s.quant.service import QuantService
from al1s.trading_admin.contracts import AdminTradingSource


def command(action, args=()):
    return QuantCommand(CommandSource(message_id="1", scope_kind="qq_group", target_id="123",
                                      sender_id="456"), action, args, 1, 1790000000,
                        settings=QuantSettings(initial_cash=Decimal("100000")),
                        command_words=tuple(DEFAULT_COMMANDS.items()))


def test_admin_account_reads_fill_only_missing_stock_names_and_keep_order_history():
    accounts, orders, source, instruments = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    accounts.get.return_value = object()
    accounts.positions.return_value = [
        SimpleNamespace(current_code='300750', market='SZ', name='300750', quantity=100, available=100, total_cost=Decimal('10')),
        SimpleNamespace(current_code='600000', market='SH', name='浦发银行', quantity=100, available=100, total_cost=Decimal('20')),
    ]
    orders.history.return_value = [SimpleNamespace(id=uuid4(), code='300750', market='SZ', name='300750',
        side='buy', quantity=100, status='filled', filled_quantity=100, total_fees=Decimal('0'),
        quote_provider='sina', execution_provider='sina', created_at=datetime.now(UTC))]
    source.security_names.return_value = {('SZ', '300750'): '宁德时代'}
    service = QuantService(accounts, orders, name_source=source, instruments=instruments)
    admin = AdminTradingSource(sender_id='123', target_id='123', message_id='a' * 32)
    positions = service.query_for_ai(admin, QuantSettings(), 'positions', 1)['data']['positions']
    assert [(row['code'], row['name']) for row in positions] == [('300750', '宁德时代'), ('600000', '浦发银行')]
    source.security_names.assert_called_once_with([('SZ', '300750')])
    instruments.cache_names.assert_called_once_with({('SZ', '300750'): '宁德时代'})
    history = service.query_for_ai(admin, QuantSettings(), 'trades', 1)['data']['trades']
    assert history[0]['code'] == '300750' and history[0]['name'] == '宁德时代'


def test_admin_account_reads_keep_codes_when_names_are_unavailable():
    accounts, orders, source, instruments = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    accounts.get.return_value = object()
    accounts.positions.return_value = [SimpleNamespace(current_code='300750', market='SZ', name='300750',
        quantity=100, available=100, total_cost=Decimal('10'))]
    source.security_names.return_value = {}
    service = QuantService(accounts, orders, name_source=source, instruments=instruments)
    admin = AdminTradingSource(sender_id='123', target_id='123', message_id='a' * 32)
    rows = service.query_for_ai(admin, QuantSettings(), 'positions', 1)['data']['positions']
    assert rows[0]['code'] == '300750' and rows[0]['name'] is None
    instruments.cache_names.assert_not_called()


def test_open_and_help_use_only_sender_and_current_command_words():
    account = Account(uuid4(), "456", Decimal("100000"), Decimal("100000"), "active", 1)
    accounts, orders = MagicMock(), MagicMock()
    accounts.open.return_value = (account, True)
    service = QuantService(accounts, orders)
    assert "100000" in service.execute(uuid4(), command("open"))
    accounts.open.assert_called_once_with("456", Decimal("100000"))
    assert "模拟买入" in service.execute(uuid4(), command("help"))
    assert "不接受" in service.execute(uuid4(), command("open", ("99999",)))


def test_asset_summary_does_not_claim_total_when_quotes_are_missing():
    account = Account(uuid4(), "456", Decimal("5000"), Decimal("100000"), "active", 1)
    accounts, orders = MagicMock(), MagicMock()
    accounts.summary.return_value = AccountSummary(account, 2, 1, None, None)
    result = QuantService(accounts, orders).execute(uuid4(), command("balance"))
    assert "5000" in result and "行情不完整" in result


def test_help_explains_commands_with_current_words_without_accessing_accounts():
    accounts, orders = MagicMock(), MagicMock()
    words = {action: f"自定义{action}" for action in DEFAULT_COMMANDS}
    help_command = replace(command("help"), command_words=tuple(words.items()))
    result = QuantService(accounts, orders).execute(uuid4(), help_command)
    assert all(word in result for word in words.values())
    assert "自定义buy 600000 100" in result and "100股" in result
    assert "自定义positions 2" in result and "自定义orders 2" in result
    assert "自定义backtest 双均线 600000 2025-01-01 2025-12-31" in result
    assert "默认日线、不复权，双均线默认5/20" in result
    assert "1分钟仅支持不复权" in result
    assert "已提交不代表已成交" in result
    assert "模拟买入" not in result
    assert accounts.mock_calls == [] and orders.mock_calls == []


def test_fees_query_shows_percentages_from_snapshot_without_account_or_market_access():
    accounts,orders=MagicMock(),MagicMock()
    settings=QuantSettings(commission_rate=Decimal('0.00000100'),
        minimum_commission=Decimal('5.0000'),stamp_tax_rate=Decimal('0.05000000'),
        slippage=Decimal('0E-8'))
    request=replace(command('help',('费率',)),settings=settings)
    result=QuantService(accounts,orders).execute(uuid4(),request)
    assert '0.0001%' in result and '5元' in result and '5%' in result and '0%' in result
    assert '仅卖出' in result and '提交时' in result
    assert '0E-8' not in result and '0.05000000' not in result
    assert accounts.mock_calls==[] and orders.mock_calls==[]


def test_asset_summary_uses_complete_fresh_fallback_without_mixing_sources():
    account = Account(uuid4(), "456", Decimal("5000"), Decimal("100000"), "active", 1)
    accounts, orders = MagicMock(), MagicMock()
    fresh = datetime.now(UTC)
    accounts.summary.side_effect = [AccountSummary(account, 2, 2, Decimal("900"), fresh - timedelta(hours=1)),
                                    AccountSummary(account, 2, 2, Decimal("1000"), fresh)]
    result = QuantService(accounts, orders).execute(uuid4(), command("balance"))
    assert "腾讯" in result and "6000" in result and "已过期" not in result
    assert [call.kwargs["provider"] for call in accounts.summary.call_args_list] == ["sina", "tencent"]
