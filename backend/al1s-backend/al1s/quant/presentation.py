from decimal import Decimal

from al1s.trading_replies.render import render, reply

ORDER_STATUS = {
    "accepted": "已受理",
    "processing": "处理中",
    "partially_filled": "部分成交",
    "filled": "已成交",
    "rejected": "已拒绝",
    "expired": "已过期",
    "unknown": "待核实",
}


def decimal_text(value) -> str:
    number = Decimal(str(value))
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def order_receipt(order, instrument, reference_price, *, created: bool, orders_word: str, command=None) -> str:
    name = instrument.name
    side = "买入" if order.side == "buy" else "卖出"
    status = ORDER_STATUS.get(order.status, "待核实")
    heading = f"{side}委托{status}" if created else f"{side}委托已存在，当前{status}"
    price = order.quote_price if order.quote_price is not None else reference_price
    pending = "尚未成交；" if order.status == "accepted" else ""
    actual_name = name if name and name != instrument.current_code else None
    values = dict(heading=heading, stock_name=actual_name,
                  stock_label=f"{actual_name}（{instrument.current_code}）" if actual_name else f"股票 {instrument.current_code}",
                  code=instrument.current_code, quantity=order.quantity, reference_price=price,
                  order_id=str(order.id)[:8], pending=pending, orders_command=orders_word,
                  side=side, status=status)
    return reply(command, "main", values) if command is not None else render(
        "quant", order.side if order.side in {"buy", "sell"} else "buy", "main", values)
