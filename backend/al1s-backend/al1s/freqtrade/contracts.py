from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.contracts import Money, QqId, QuantSettings, Rate
from al1s.trading_admin.contracts import AdminTradingSource

SPOT_COMMANDS = dict(
    open="币开户",
    buy="币买入",
    sell="币卖出",
    balance="币资产",
    positions="币持仓",
    orders="币成交",
    help="币帮助",
)
FUTURES_COMMANDS = dict(
    f_open="合约开户",
    long="合约开多",
    short="合约开空",
    close="合约平仓",
    f_balance="合约资产",
    f_positions="合约持仓",
    f_orders="合约成交",
)
CHART_COMMANDS = dict(chart="币走势", f_chart="合约走势")
CAPITAL_COMMANDS = dict(deposit="币充值", f_deposit="合约充值", f_margin="合约加保证金")
DEFAULT_COMMANDS = {**SPOT_COMMANDS, **FUTURES_COMMANDS, **CHART_COMMANDS, **CAPITAL_COMMANDS}
FUTURES_ACTIONS = frozenset({*FUTURES_COMMANDS, "f_chart", "f_deposit", "f_margin"})
PAIRS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
FUTURES_PAIRS = tuple(pair + ":USDT" for pair in PAIRS)
Market = Literal["spot", "futures"]
WRITE_ACTIONS = frozenset({"buy", "sell", "long", "short", "close", *CAPITAL_COMMANDS})


class FreqtradeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    private_enabled: bool = False
    event_ws_url: str | None = None
    groups: list[QqId] = Field(default_factory=list, max_length=100)
    initial_cash: Money = Field(default=Decimal("1000"), gt=0)
    fee: Rate = Decimal("0.001")
    reply_interval_seconds: int = Field(default=10, ge=1, le=3600)
    max_active_accounts: int = Field(default=5, ge=1, le=50)
    idle_seconds: int = Field(default=300, ge=60, le=86400)
    futures_enabled: bool = False
    futures_initial_cash: Money = Field(default=Decimal("1000"), gt=0)
    futures_fee: Rate = Decimal("0.0005")
    futures_default_leverage: int = Field(default=3, ge=1, le=150)
    futures_max_leverage: int = Field(default=0, ge=0, le=150)

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        QuantSettings(enabled=self.enabled, event_ws_url=self.event_ws_url, groups=self.groups)
        if self.futures_max_leverage and self.futures_default_leverage > self.futures_max_leverage:
            raise ValueError("freqtrade_leverage_invalid")
        return self


class FreqtradeRecord(BaseModel):
    settings: FreqtradeSettings = Field(default_factory=FreqtradeSettings)
    commands: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_COMMANDS))
    row_version: int = 0
    reply_templates: dict[str, dict[str, str]] = Field(default_factory=dict)


class FreqtradeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: CommandSource | AdminTradingSource
    action: Literal[
        "open",
        "buy",
        "sell",
        "balance",
        "positions",
        "orders",
        "help",
        "f_open",
        "long",
        "short",
        "close",
        "f_balance",
        "f_positions",
        "f_orders",
        "chart",
        "f_chart",
        "deposit",
        "f_deposit",
        "f_margin",
    ]
    arguments: tuple[str, ...] = ()
    event_timestamp: int = Field(gt=0)
    settings_version: int = Field(ge=1)
    mapping_version: int = Field(ge=1)
    normalized_keyword: str
    settings: FreqtradeSettings
    commands: dict[str, str]
    reply_templates: dict[str, dict[str, str]] = Field(default_factory=dict)

    @property
    def command_words(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.commands.items())

    @property
    def market(self) -> Market:
        return "futures" if self.action in FUTURES_ACTIONS else "spot"


class FreqtradeReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: CommandSource | AdminTradingSource
    market: Literal["spot", "futures"]
    kind: Literal["balance", "positions", "trades"]
    page: int = Field(default=1, ge=1, le=1000)


class FreqtradeChartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: CommandSource
    market: Literal["spot", "futures"]
    symbol: Literal["BTC", "ETH", "SOL"]
    period: Literal["15分钟", "1小时", "4小时", "1天", "7天"] = "1小时"
