from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.freqtrade.charts import Charts
from al1s.freqtrade.service import FreqtradeService
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.contracts import QuantSettings
from al1s.quant.gateway import QuantGateway
from al1s.quant.service import QuantService
from tests.test_freqtrade_service import command as ft_command
from tests.test_quant_gateway import event
from tests.test_quant_service import command as quant_command


def test_message_freezes_templates_even_when_configuration_is_later_changed():
    gateway, service = QuantGateway(), uuid4()
    record = QuantSettingsRecord(
        settings=QuantSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["123456"]),
        commands=DEFAULT_COMMANDS,
        row_version=1,
        mapping_version=1,
        reply_templates={"buy": {"main": "旧格式 {stock_name}"}},
    )
    gateway.activate(service, record)
    captured = gateway.dispatch(service, event("模拟买入 300750 100"), set())
    record.reply_templates["buy"]["main"] = "新格式"
    assert captured.reply_templates["buy"]["main"] == "旧格式 {stock_name}"
    captured.reply_templates["buy"]["main"] = "局部修改"
    assert (
        gateway.dispatch(service, event("模拟买入 300750 100"), set()).reply_templates["buy"][
            "main"
        ]
        == "旧格式 {stock_name}"
    )


def test_quant_normal_reply_is_customizable_but_invalid_arguments_remain_system_text():
    accounts = MagicMock()
    accounts.open.return_value = SimpleNamespace(cash=12345), True
    service = QuantService(accounts, MagicMock())
    cmd = replace(
        quant_command("open"), reply_templates={"open": {"main": "开户成功\n余额 {cash}元"}}
    )
    assert service.execute(uuid4(), cmd) == "开户成功\n余额 12345元"
    invalid = replace(cmd, arguments=("别人的QQ",))
    assert "不接受参数" in service.execute(uuid4(), invalid)


def test_freqtrade_trade_stores_rendered_reply_and_never_reexecutes_replay():
    repo, manager = MagicMock(), MagicMock()
    receipt = SimpleNamespace(id=uuid4(), result=None)
    repo.claim.return_value = receipt, True
    profile = SimpleNamespace(id=uuid4(), state="ready", service_id=uuid4())
    repo.profile.return_value = profile
    repo.get.return_value = SimpleNamespace(settings=ft_command().settings)
    manager.ensure.return_value.call.side_effect = [[], {"trade_id": 7}]
    cmd = ft_command().model_copy(
        update={"reply_templates": {"buy": {"main": "{pair} #{trade_id}\n{amount_text}"}}}
    )
    service = FreqtradeService(repo, manager)
    result = service.execute(uuid4(), cmd)
    assert result == "BTC/USDT #7\n100 USDT"
    repo.complete.assert_called_once_with(receipt.id, result)
    repo.claim.return_value = SimpleNamespace(result=result), False
    assert service.execute(uuid4(), cmd) == result
    assert manager.ensure.call_count == 1


def test_freqtrade_list_uses_row_template_without_more_native_calls():
    service = FreqtradeService(MagicMock(), MagicMock())
    native = MagicMock()
    native.call.return_value = [
        {"trade_id": 1, "pair": "BTC/USDT", "amount": 0.01, "stake_amount": 100}
    ]
    cmd = ft_command("positions", ()).model_copy(
        update={
            "reply_templates": {
                "positions": {"main": "我的持仓\n{rows}", "row": "{pair}：{amount}币"}
            }
        }
    )
    assert service.query(native, cmd, SimpleNamespace()) == "我的持仓\nBTC/USDT：0.01币"
    assert native.call.call_count == 1


def test_chart_template_changes_caption_and_keeps_generated_image(monkeypatch):
    from datetime import UTC, datetime

    import al1s.freqtrade.charts as module

    chart = Charts()
    chart.exchange = object()
    data = dict(
        symbol="BTC",
        period="1小时",
        interval="1m",
        collected_at=datetime.now(UTC),
        times=[datetime.now(UTC), datetime.now(UTC)],
        prices=[100, 101],
    )
    monkeypatch.setattr(module, "fetch_chart", lambda *args: data)
    monkeypatch.setattr(module, "render_chart", lambda _: b"example-png")
    cmd = ft_command("chart", ("BTC",)).model_copy(
        update={"reply_templates": {"chart": {"main": "{symbol} 最近{period}"}}}
    )
    result = chart.execute(cmd)
    assert result["content"] == "BTC 最近1小时"
    assert result["image"] == "ZXhhbXBsZS1wbmc="


def test_asset_warning_is_preserved_within_length_limit_without_requery():
    from decimal import Decimal
    from al1s.adapters.postgres.quant_accounts import Account, AccountSummary

    accounts = MagicMock()
    account = Account(uuid4(), "456", Decimal(1000), Decimal(1000), "unknown", 1)
    accounts.summary.return_value = AccountSummary(account, 0, 0, Decimal(0), None)
    cmd = replace(quant_command("balance"), reply_templates={"balance": {"main": "X" * 4096}})
    result = QuantService(accounts, MagicMock()).execute(uuid4(), cmd)
    assert len(result) <= 4096 and "账户状态待核实" in result and "模拟资金" in result
    assert accounts.summary.call_count == 1


def test_recovery_prefix_is_included_in_the_reply_size_budget():
    native = MagicMock()
    native.call.side_effect = [[dict(trade_id=1, pair="BTC/USDT:USDT", amount=1)], {"trades": []}]
    service = FreqtradeService(MagicMock(), MagicMock())
    service.reconcile = lambda *args: "仍有待核实操作，写入保护保持。\n"
    cmd = ft_command("f_orders", ()).model_copy(
        update={"reply_templates": {"f_orders": {"main": "X" * 4096}}}
    )
    result = service.query(native, cmd, SimpleNamespace(state="unknown"))
    assert len(result) <= 4096 and result.startswith("仍有待核实操作")
    assert "#1 BTC/USDT:USDT" in result


def test_missing_security_name_is_not_exposed_as_a_real_name_variable():
    from al1s.quant.presentation import order_receipt

    request = replace(
        quant_command("buy"), reply_templates={"buy": {"main": "{stock_name}\n{stock_label}"}}
    )
    result = order_receipt(
        SimpleNamespace(id=uuid4(), side="buy", status="accepted", quantity=100, quote_price=10),
        SimpleNamespace(name=None, current_code="300750"),
        10,
        created=True,
        orders_word="模拟成交",
        command=request,
    )
    assert result == "未提供\n股票 300750"
