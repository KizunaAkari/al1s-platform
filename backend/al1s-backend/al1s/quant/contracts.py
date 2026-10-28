from decimal import Decimal
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QqId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
Money = Annotated[Decimal, Field(max_digits=20, decimal_places=4)]
Rate = Annotated[Decimal, Field(ge=0, le=1, max_digits=12, decimal_places=8)]
Provider = Literal["sina", "tencent", "eastmoney", "tushare", "gugudata"]
DEFAULT_PROVIDER_ORDER = ("sina", "tencent", "eastmoney", "tushare", "gugudata")


class QuantSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    private_enabled: bool = False
    event_ws_url: str | None = Field(default=None, max_length=1024)
    groups: list[QqId] = Field(default_factory=list, max_length=100)
    initial_cash: Money = Field(default=Decimal("100000"), gt=0)
    commission_rate: Rate = Decimal(0)
    minimum_commission: Money = Field(default=Decimal(0), ge=0)
    stamp_tax_rate: Rate = Decimal(0)
    slippage: Rate = Decimal(0)
    max_quote_age_seconds: int = Field(default=120, ge=1, le=3600)
    bar_cache_days: int = Field(default=30, ge=1, le=365)
    refresh_seconds: int = Field(default=60, ge=10, le=3600)
    reply_interval_seconds: int = Field(default=10, ge=1, le=3600)
    provider: Provider = "sina"
    provider_order: list[Provider] = Field(default_factory=lambda: list(DEFAULT_PROVIDER_ORDER))

    @model_validator(mode="before")
    @classmethod
    def legacy_provider_order(cls, value):
        if isinstance(value, dict) and "provider_order" not in value:
            primary = value.get("provider", "sina")
            if primary in DEFAULT_PROVIDER_ORDER:
                value = {
                    **value,
                    "provider_order": [
                        primary,
                        *(p for p in DEFAULT_PROVIDER_ORDER if p != primary),
                    ],
                }
        return value

    @model_validator(mode="after")
    def require_connection_when_enabled(self) -> Self:
        if self.enabled and not self.event_ws_url:
            raise ValueError("quant_event_url_required")
        if (
            len(self.provider_order) != 5
            or len(set(self.provider_order)) != 5
            or self.provider_order[0] != self.provider
        ):
            raise ValueError("quant_provider_order_invalid")
        return self

    @field_validator("groups")
    @classmethod
    def unique_groups(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("quant_group_duplicate")
        return value

    @field_validator("event_ws_url")
    @classmethod
    def valid_event_url(cls, value: str | None) -> str | None:
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"ws", "wss"}
                or not parsed.hostname
                or any((parsed.username, parsed.password, parsed.query, parsed.fragment))
            ):
                raise ValueError("quant_event_url_invalid")
        return value
