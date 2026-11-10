import json
from decimal import Decimal

from freqtrade.persistence import Trade
from freqtrade.persistence.custom_data import _CustomData
from freqtrade.persistence.key_value_store import ValueTypesEnum, _KeyValueStoreModel
from sqlalchemy import func, select

CASH_KEY = "al1s_cash_total_v1"
MARGIN_KEY = "al1s_added_margin_v1"


def transaction_lock():
    if Trade.session.get_bind().dialect.name == "postgresql":
        Trade.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(func.current_schema()), 0x414C3153))
        )


def key_row(key):
    return Trade.session.execute(
        select(_KeyValueStoreModel).where(_KeyValueStoreModel.key == key)
    ).scalar_one_or_none()


def cash_total() -> Decimal:
    row = key_row(CASH_KEY)
    return Decimal(row.string_value) if row is not None else Decimal(0)


def save_cash(value: Decimal):
    row = key_row(CASH_KEY) or _KeyValueStoreModel(key=CASH_KEY)
    row.value_type, row.string_value = ValueTypesEnum.STRING, format(value, ".4f")
    Trade.session.add(row)


def open_margins():
    rows = Trade.session.execute(
        select(_CustomData.ft_trade_id, Trade.pair, Trade.stake_amount, _CustomData.cd_value)
        .join(Trade, Trade.id == _CustomData.ft_trade_id)
        .where(_CustomData.cd_key == MARGIN_KEY, Trade.is_open.is_(True), Trade.amount > 0)
    ).all()
    return {
        identity: dict(pair=pair, stake=Decimal(str(stake)), extra=Decimal(json.loads(value)))
        for identity, pair, stake, value in rows
    }


def save_margin(trade_id: int, value: Decimal):
    row = Trade.session.scalar(
        select(_CustomData).where(
            _CustomData.ft_trade_id == trade_id, _CustomData.cd_key == MARGIN_KEY
        )
    )
    if row is None:
        row = _CustomData(ft_trade_id=trade_id, cd_key=MARGIN_KEY, cd_type="str")
    row.cd_value = json.dumps(format(value, ".4f"))
    Trade.session.add(row)


def operation_key(operation_id):
    return "al1s_cap_" + operation_id.hex


def operation_record(operation_id):
    pointer = key_row(operation_key(operation_id))
    if pointer is None:
        return None
    row = Trade.session.get(_CustomData, pointer.int_value)
    if row is None:
        raise ValueError("原生资金凭据不完整，保留写入保护。")
    return json.loads(row.cd_value)


def save_operation(body, request, result):
    row = _CustomData(
        ft_trade_id=body.trade_id,
        cd_key=operation_key(body.operation_id),
        cd_type="dict",
        cd_value=json.dumps(dict(request=request, result=result)),
    )
    Trade.session.add(row)
    Trade.session.flush()
    Trade.session.add(
        _KeyValueStoreModel(
            key=operation_key(body.operation_id), value_type=ValueTypesEnum.INT, int_value=row.id
        )
    )
