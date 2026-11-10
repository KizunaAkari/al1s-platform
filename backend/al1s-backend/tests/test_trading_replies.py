from decimal import Decimal

import pytest

from al1s.trading_replies.catalog import catalogue
from al1s.trading_replies.render import preview, render, render_list, validate_templates


def test_catalogue_covers_current_actions_and_uses_actual_fields():
    quant = catalogue("quant")
    futures = catalogue("freqtrade")
    assert len(quant) == 8 and len(futures) == 19
    assert {item["group"] for item in futures.values()} == {"现货", "合约", "通用"}
    assert "stock_name" in quant["buy"]["variants"]["main"]["variables"]
    assert "total_equity" not in futures["balance"]["variants"]["main"]["variables"]
    assert "unrealized_pnl" not in futures["balance"]["variants"]["main"]["variables"]
    assert "stake_amount" not in futures["close"]["variants"]["main"]["variables"]
    assert "leverage" not in futures["close"]["variants"]["main"]["variables"]


def test_multiline_template_keeps_literals_and_formats_numbers_without_recursing():
    text = render(
        "quant",
        "buy",
        "main",
        {
            "stock_name": "宁德时代",
            "code": "300750",
            "quantity": 1000,
            "reference_price": Decimal("225.800000"),
            "status": "已受理",
            "order_id": "6a91e8d3",
            "orders_command": "查单",
            "side": "买入",
            "heading": "买入委托已受理",
            "pending": "尚未成交；",
        },
        {"main": "{{模拟}}\n{stock_name}({code}) {quantity}股\n{reference_price}元 {status}"},
    )
    assert text == "{模拟}\n宁德时代(300750) 1000股\n225.8元 已受理"
    assert (
        render("quant", "buy", "main", {"stock_name": "{code}"}, {"main": "{stock_name}"})
        == "{code}"
    )


@pytest.mark.parametrize(
    "body",
    [
        "{no_such_value}",
        "{stock_name.__class__}",
        "{quantity[0]}",
        "{quantity:999999}",
        "{code!r}",
        "{",
    ],
)
def test_template_rejects_unknown_fields_and_expressions(body):
    with pytest.raises(ValueError):
        validate_templates("quant", "buy", {"main": body})


def test_render_failure_falls_back_to_default_without_executing_any_business_code():
    values = {
        "heading": "买入委托已受理",
        "stock_name": "宁德时代",
        "code": "300750",
        "quantity": 1000,
        "reference_price": Decimal("225.800000"),
        "order_id": "6a91e8d3",
        "pending": "尚未成交；",
        "orders_command": "查单",
    }
    original = render("quant", "buy", "main", values)
    assert render("quant", "buy", "main", values, {"main": "x" * 4097}) == original
    assert render("quant", "buy", "main", values, {"main": "{status}"}) == original


def test_preview_uses_only_examples_and_current_keyword_and_row_template():
    text = preview("freqtrade", "positions", "我的持仓", {"row": "{pair}\n{amount}币"}, "row")
    assert "BTC/USDT" in text and "\n0.01币" in text
    assert "模拟持仓" in text
    assert preview("quant", "help", "使用帮助", {}, "fees").startswith("模拟交易费率")


def test_zero_fee_and_missing_native_values_are_distinct():
    value = render(
        "freqtrade",
        "f_positions",
        "row",
        {
            "trade_id": 1,
            "pair": "BTC/USDT:USDT",
            "added_margin": Decimal("0E-8"),
            "profit_abs": None,
        },
        {"row": "{added_margin}/{profit_abs}"},
    )
    assert value == "0/未提供"


def test_long_custom_rows_fall_back_to_complete_default_rows():
    rows = [dict(trade_id=i, pair="BTC/USDT", amount=0.01, stake_amount=100) for i in range(20)]
    value = render_list("freqtrade", "positions", {"page": 1}, rows, {"row": "很长" * 1500})
    assert len(value) <= 4096 and "#0 BTC/USDT" in value and "#19 BTC/USDT" in value


def test_every_catalogue_result_can_preview_with_only_its_declared_example_fields():
    for engine in ("quant", "freqtrade"):
        for action, item in catalogue(engine).items():
            for key, spec in item["variants"].items():
                validate_templates(engine, action, {key: spec["default_template"]})
                result = preview(engine, action, "示例指令", {}, key)
                assert result and len(result) <= 4096, (engine, action, key)
                assert "未提供" not in result, (engine, action, key)


@pytest.mark.parametrize("action,amount", [("buy", "123.45 USDT"), ("sell", "0.002币")])
def test_default_spot_receipt_exposes_requested_amount_without_claiming_a_fill(action, amount):
    result = render("freqtrade", action, "main", dict(
        pair="ETH/USDT", side="买入" if action == "buy" else "卖出", trade_id=7,
        amount_text=amount, orders_command="查我的成交", fee_percent=Decimal("0.1"),
    ))
    assert amount in result and "查我的成交" in result
    assert "提交不代表成交" in result
    assert "已成交" not in result


def test_futures_history_shows_state_and_does_not_assume_closed_positions():
    row = dict(catalogue("freqtrade")["f_orders"]["variants"]["row"]["sample"])
    row.update(state="委托处理中", exit_reason="持仓/委托中", profit_abs=None)
    result = render_list("freqtrade", "f_orders", dict(page=2, market_label="合约"), [row])
    assert "委托处理中" in result and "第2页" in result and "合约" in result
    assert "未提供" in result and "已平仓" not in result


@pytest.mark.parametrize("engine,action,count", [
    ("quant", "positions", 25), ("quant", "orders", 25),
    ("freqtrade", "positions", 20), ("freqtrade", "orders", 20),
    ("freqtrade", "f_positions", 10), ("freqtrade", "f_orders", 10),
])
def test_default_full_page_preserves_all_records_and_page_number(engine, action, count):
    spec = catalogue(engine)[action]
    rows = []
    for index in range(count):
        row = dict(spec["variants"]["row"]["sample"])
        row.update(code=f"{600000 + index}", trade_id=index + 1, order_id=f"{index + 1:08d}")
        rows.append(row)
    result = render_list(engine, action, dict(page=3, market_label=spec["group"]), rows)
    assert "第3页" in result and len(result) <= 4096
    first, last = ("600000", str(600000 + count - 1)) if engine == "quant" else ("#1 ", f"#{count} ")
    assert first in result and last in result
