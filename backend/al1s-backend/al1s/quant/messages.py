from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.commands import QuantCommand, normalize_keyword, validate_mappings
from al1s.quant.contracts import QuantSettings
from al1s.trading_admin.contracts import AdminTradingSource


class QuantCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: CommandSource
    action: Literal["open", "buy", "sell", "balance", "positions", "orders", "backtest", "help"]
    arguments: list[Annotated[str, Field(max_length=128)]] = Field(max_length=8)
    mapping_version: int = Field(ge=1)
    settings_version: int = Field(ge=1)
    event_timestamp: int = Field(ge=1)
    normalized_keyword: str = Field(min_length=1, max_length=32)
    settings: QuantSettings
    commands: dict[str, str]
    reply_templates: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def authorized_snapshot(self) -> Self:
        words = validate_mappings(self.commands, set())
        if (
            not self.settings.enabled
            or self.source.guild_id is not None
            or self.source.scope_kind not in {"qq_group", "qq_private"}
            or (
                self.source.scope_kind == "qq_group"
                and self.source.target_id not in self.settings.groups
            )
            or (
                self.source.scope_kind == "qq_private"
                and (
                    not self.settings.private_enabled
                    or self.source.target_id != self.source.sender_id
                )
            )
            or normalize_keyword(self.normalized_keyword) != words[self.action]
        ):
            raise ValueError("quant_command_not_authorized")
        return self

    def command(self) -> QuantCommand:
        return QuantCommand(
            self.source,
            self.action,
            tuple(self.arguments),
            self.mapping_version,
            self.event_timestamp,
            self.normalized_keyword,
            self.settings_version,
            self.settings,
            tuple(self.commands.items()),
            self.reply_templates,
        )


class QuantReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: CommandSource
    kind: Literal["balance", "positions", "trades"]
    page: int = Field(default=1, ge=1, le=1000)


class QuantAdminCommandRequest(QuantCommandRequest):
    source: AdminTradingSource
    action: Literal["open", "buy", "sell"]

    @model_validator(mode="after")
    def authorized_snapshot(self) -> Self:
        words = validate_mappings(self.commands, set())
        if not self.settings.enabled or normalize_keyword(self.normalized_keyword) != words[self.action]:
            raise ValueError("quant_command_not_authorized")
        return self


class QuantAdminReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: AdminTradingSource
    kind: Literal["balance", "positions", "trades"]
    page: int = Field(default=1, ge=1, le=1000)
