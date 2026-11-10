from decimal import Decimal
from uuid import UUID

from al1s.adapters.postgres.quant_instruments import Instrument
from al1s.adapters.postgres.quant_orders import Order
from al1s.quant.presentation import decimal_text, order_receipt


def test_receipt_identifies_stock_and_quantity_without_internal_details():
    instrument = Instrument(UUID(int=1), "SZ", "300750", "main", name="宁德时代")
    order = Order(
        UUID("6a91e8d3-0000-0000-0000-000000000000"),
        "accepted",
        "buy",
        1000,
        quote_price=Decimal("225.800000"),
    )
    result = order_receipt(order, instrument, Decimal("1"), created=True, orders_word="查委托")
    assert "宁德时代（300750）" in result and "买入" in result and "1000股" in result
    assert "225.8元" in result and "6a91e8d3" in result
    assert "未成交" in result and "查委托" in result
    assert not any(
        word in result for word in ("accepted", "来源", "时间", "佣金", "税", "滑点", "225.800000")
    )


def test_replay_uses_original_order_state_quantity_and_price():
    instrument = Instrument(UUID(int=1), "SH", "600000", "main")
    order = Order(UUID(int=2), "filled", "sell", 200, quote_price=Decimal("10.500000"))
    result = order_receipt(order, instrument, Decimal("99"), created=False, orders_word="模拟成交")
    assert "卖出" in result and "200股" in result and "10.5元" in result
    assert "600000" in result and "已存在" in result and "已成交" in result
    assert "99元" not in result


def test_decimal_text_preserves_significant_digits_and_removes_exponents():
    assert decimal_text(Decimal("225.800000")) == "225.8"
    assert decimal_text(Decimal("0E-8")) == "0"
    assert decimal_text(Decimal("100000")) == "100000"
    assert decimal_text(Decimal("0.00000100")) == "0.000001"
