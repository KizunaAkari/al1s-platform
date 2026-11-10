from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Market = Literal["a-share", "spot", "futures"]


class AdminTradingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_kind: Literal["admin"] = "admin"
    sender_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    target_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    message_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    guild_id: None = None

    @model_validator(mode="after")
    def same_owner(self):
        if self.sender_id != self.target_id:
            raise ValueError("trading_owner_mismatch")
        return self


class TradingOwnerWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qq_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")


class TradingActionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_version: int = Field(ge=1, strict=True)
    action: Literal["open", "buy", "sell", "long", "short", "close", "deposit", "margin"]
    symbol: str | None = Field(default=None, max_length=16)
    amount: str | None = Field(default=None, max_length=32)
    quantity: int | None = Field(default=None, gt=0, le=999999999, strict=True)
    leverage: int | None = Field(default=None, ge=1, le=150, strict=True)
    all: bool = Field(default=False, strict=True)

    def arguments(self, market: Market) -> tuple[str, ...]:
        action = self.action
        allowed = {"a-share": {"open", "buy", "sell"},
                   "spot": {"open", "buy", "sell", "deposit"},
                   "futures": {"open", "long", "short", "close", "deposit", "margin"}}
        if action not in allowed[market]:
            raise ValueError("trading_action_invalid")
        if action == "open":
            if self.symbol or self.amount or self.quantity is not None or self.leverage is not None or self.all:
                raise ValueError("trading_action_invalid")
            return ()
        if market == "a-share":
            if (not self.symbol or not self.symbol.isascii() or not self.symbol.isdigit() or len(self.symbol) != 6
                    or self.quantity is None or self.amount is not None or self.leverage is not None or self.all):
                raise ValueError("trading_action_invalid")
            return self.symbol, str(self.quantity)
        if self.quantity is not None or (self.leverage is not None and action not in {"long", "short"}):
            raise ValueError("trading_action_invalid")
        if action == "deposit":
            if self.symbol or self.all or self.leverage is not None:
                raise ValueError("trading_action_invalid")
        elif not self.symbol or self.symbol.upper() not in {"BTC", "ETH", "SOL"}:
            raise ValueError("trading_action_invalid")
        if self.all:
            if action not in {"sell", "close"} or self.amount is not None:
                raise ValueError("trading_action_invalid")
            amount = "全部"
        else:
            try:
                number = Decimal(self.amount or "")
                if not number.is_finite() or not 0 < number <= 10**12 or number.as_tuple().exponent < -12:
                    raise ValueError
                if action in {"deposit", "margin"} and (number > 10**9 or number.as_tuple().exponent < -4):
                    raise ValueError
            except (InvalidOperation, ValueError) as error:
                raise ValueError("trading_action_invalid") from error
            amount = self.amount or ""
        if action == "deposit":
            return (amount,)
        args = (self.symbol.upper(), amount)
        return (*args, str(self.leverage)) if self.leverage is not None else args
