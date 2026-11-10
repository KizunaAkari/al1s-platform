from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from al1s.freqtrade.commands import parse_order
from al1s.freqtrade.presentation import number
from al1s.trading_replies.render import reply

CAPITAL_ACTIONS = frozenset({"deposit", "f_deposit", "f_margin"})


def parse_capital(action, arguments):
    margin = action == "f_margin"
    if len(arguments) != (2 if margin else 1):
        raise ValueError("充值参数为金额；追加参数为币种 金额，例如BTC 100。")
    try:
        amount = Decimal(arguments[-1])
        if (
            not amount.is_finite()
            or not 0 < amount <= 10**9
            or amount != amount.quantize(Decimal("0.0001"))
        ):
            raise ValueError
    except (ValueError, InvalidOperation) as error:
        raise ValueError("金额须大于0、最多4位小数，每次最多10亿虚拟USDT。") from error
    pair = None
    if margin:
        symbol = arguments[0].upper().removesuffix(":USDT")
        pair = parse_order("buy", (symbol, str(amount)))[0] + ":USDT"
    return pair, amount


def receipt_matches(pending, receipt):
    try:
        return (
            receipt.get("operation_id") == str(pending.id)
            and receipt.get("action") == ("margin" if pending.action == "f_margin" else "deposit")
            and Decimal(str(receipt.get("amount"))) == pending.capital_amount
            and receipt.get("trade_id") == pending.native_trade_id
        )
    except (InvalidOperation, TypeError):
        return False


def execute_capital(service, native, command, request_id):
    pair, amount = parse_capital(command.action, command.arguments)
    current = None
    if pair:
        try:
            rows = native.call("status")
        except (OSError, ValueError) as error:
            raise ValueError("无法核验持仓，本次未发送追加保证金操作，请稍后重试。") from error
        current = next((row for row in rows if row["pair"] == pair), None)
        if current is None:
            raise ValueError("没有该合约的已有持仓，追加保证金不会自动开仓。")
        if current.get("has_open_orders"):
            raise ValueError("该合约有在途委托，待成交后再追加保证金。")
    trade_id = current["trade_id"] if current else None
    service.repository.prepare_capital(request_id, amount, trade_id)
    result = native.call(
        "al1s/capital",
        dict(
            operation_id=str(request_id),
            action="margin" if pair else "deposit",
            amount=str(amount),
            trade_id=trade_id,
        ),
    )
    pending = SimpleNamespace(
        id=request_id, action=command.action, capital_amount=amount, native_trade_id=trade_id
    )
    if not receipt_matches(pending, result):
        raise OSError("freqtrade_capital_result_unconfirmed")
    if pair:
        return reply(command, "main", dict(pair=pair, capital_amount=amount,
            added_total=number(result["added_margin"]), total_collateral=number(result["total_collateral"]),
            liquidation_price=number(result["liquidation_price"]), free_after=number(result["free_after"])), engine="freqtrade")
    return reply(command, "main", dict(capital_amount=amount, free_after=number(result["free_after"])), engine="freqtrade")
