from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from freqtrade.persistence import Trade
from freqtrade.rpc.api_server.api_auth import http_basic_or_jwt_token
from freqtrade.rpc.api_server.deps import get_rpc, is_trading_mode
from freqtrade.rpc.api_server.webserver import ApiServer
from freqtrade.rpc.rpc import RPC
from freqtrade.wallets import Wallets
from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.freqtrade import native_capital_store as store
from al1s.freqtrade.native_capital_wallet import (
    added_margin,
    initialize_wallet,
    update_liquidation,
    update_wallet,
    wallet_state,
)
from al1s.quant.contracts import Money

EXTENSION_VERSION = 1
_installed = False
_original_configure = ApiServer.configure_app


class CapitalOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    operation_id: UUID
    action: Literal["deposit", "margin"]
    amount: Money = Field(gt=0, le=10**9)
    trade_id: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_target(self):
        if (self.action == "margin") != (self.trade_id is not None):
            raise ValueError("capital_target_invalid")
        return self


def validate_mode(config):
    if (
        config.get("dry_run") is not True
        or config.get("stake_currency") != "USDT"
        or config.get("exchange", {}).get("name") != "binance"
        or config.get("trading_mode") not in {"spot", "futures"}
        or (config.get("trading_mode") == "futures" and config.get("margin_mode") != "isolated")
    ):
        raise ValueError("资金扩展只支持固定Binance虚拟USDT现货/逐仓模拟。")


def _change(bot, body):
    request = dict(
        operation_id=str(body.operation_id),
        action=body.action,
        amount=format(body.amount.quantize(Decimal("0.0001")), "f"),
        trade_id=body.trade_id,
    )
    store.transaction_lock()
    previous = store.operation_record(body.operation_id)
    if previous is not None:
        if previous["request"] != request:
            raise ValueError("资金操作ID参数冲突，未重复操作。")
        return previous["result"]
    bot.wallets.update(require_update=True)
    before = wallet_state(bot.wallets)
    trade = None
    if body.action == "deposit":
        total = store.cash_total() + body.amount
        if total > 10**12:
            raise ValueError("累计虚拟充值不能超过1万亿USDT。")
        store.save_cash(total)
    else:
        trade = Trade.session.get(Trade, body.trade_id)
        if (
            bot.config.get("trading_mode") != "futures"
            or trade is None
            or not trade.is_open
            or trade.amount <= 0
        ):
            raise ValueError("该逐仓持仓不存在或已退出。")
        if trade.has_open_orders:
            raise ValueError("该持仓有在途委托，未追加保证金。")
        if body.amount > Decimal(str(before["free"])):
            raise ValueError("合约可用余额不足，请先充值虚拟USDT。")
        store.save_margin(trade.id, added_margin(bot.wallets, trade) + body.amount)
    Trade.session.flush()
    bot.wallets.update(require_update=True)
    if trade is not None:
        update_liquidation(
            trade, exchange=bot.exchange, wallets=bot.wallets, stake_currency="USDT", dry_run=True
        )
    state = wallet_state(bot.wallets)
    result = dict(
        **request,
        free_before=before["free"],
        free_after=state["free"],
        deposits_total=state["deposits_total"],
        added_margin="0.0000",
        total_collateral=None,
        liquidation_price=None,
    )
    if trade is not None:
        extra = added_margin(bot.wallets, trade)
        result.update(
            added_margin=str(extra),
            total_collateral=str(Decimal(str(trade.stake_amount)) + extra),
            liquidation_price=trade.liquidation_price,
        )
    store.save_operation(body, request, result)
    Trade.commit()
    return result


def apply_operation(bot, body):
    validate_mode(bot.config)
    with bot._exit_lock, bot.wallets._al1s_lock:
        try:
            return _change(bot, body)
        except Exception:
            Trade.rollback()
            bot.wallets.update(require_update=True)
            raise


router = APIRouter()


@router.post("/al1s/capital")
def apply(body: CapitalOperation, rpc: Annotated[RPC, Depends(get_rpc)]):
    try:
        return apply_operation(rpc._freqtrade, body)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/al1s/capital")
def read_state(rpc: Annotated[RPC, Depends(get_rpc)]):
    bot = rpc._freqtrade
    validate_mode(bot.config)
    if not hasattr(bot, "_exit_lock"):
        raise HTTPException(503, "capital_initializing")
    with bot.wallets._al1s_lock:
        bot.wallets.update(require_update=True)
        return wallet_state(bot.wallets)


@router.get("/al1s/capital/operations/{identity}")
def read_operation(identity: UUID, rpc: Annotated[RPC, Depends(get_rpc)]):
    validate_mode(rpc._freqtrade.config)
    row = store.operation_record(identity)
    if row is None:
        raise HTTPException(404, "capital_operation_not_found")
    return row["result"]


def configure(self, app, config):
    app.include_router(
        router,
        prefix="/api/v1",
        dependencies=[Depends(http_basic_or_jwt_token), Depends(is_trading_mode)],
    )
    _original_configure(self, app, config)


def install():
    global _installed
    if _installed:
        return
    import freqtrade
    from freqtrade import freqtradebot
    from freqtrade.leverage import liquidation_price

    if freqtrade.__version__ != "2026.9":
        raise ValueError("capital_extension_requires_freqtrade_2026_9")
    Wallets.__init__, Wallets._update_dry = initialize_wallet, update_wallet
    freqtradebot.update_liquidation_prices = update_liquidation
    liquidation_price.update_liquidation_prices = update_liquidation
    ApiServer.configure_app = configure
    _installed = True
