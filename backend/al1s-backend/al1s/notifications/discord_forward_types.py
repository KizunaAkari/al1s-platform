"""Typed input and limits for Discord forwarding rules."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

MAX_ACTIVE_RULES = 500


@dataclass(frozen=True)
class ChannelState:
    kind: str
    enabled: bool
    deleted: bool


@dataclass(frozen=True)
class ActionInput:
    channel_id: UUID
    kind: str
    target: str
    review_policy: str


@dataclass(frozen=True)
class RuleInput:
    bot_service_id: UUID
    name: str
    guild_id: str
    channel_id: str
    trigger_kind: str
    trigger_text: str | None
    frequency_count: int | None
    frequency_window_seconds: int | None
    cooldown_seconds: int | None
    enabled: bool
    actions: tuple[ActionInput, ...]


ActionValidator = Callable[[dict[UUID, ChannelState], tuple[ActionInput, ...]], None]


class DiscordRuleRepository(Protocol):
    def list_page(
        self, bot_service_id: UUID, *, after_id: UUID | None = None, limit: int = 50
    ) -> list[dict[str, object]]: ...

    def enabled_for_worker(self, bot_service_id: UUID) -> list[dict[str, object]]: ...

    def save(
        self,
        data: RuleInput,
        *,
        rule_id: UUID | None = None,
        expected_version: int | None = None,
        validate_actions: ActionValidator,
    ) -> dict[str, object]: ...

    def delete(self, rule_id: UUID, expected_version: int) -> None: ...

    def match_actions(
        self, bot_service_id: UUID, rule_id: UUID, version: int, guild_id: str, channel_id: str
    ) -> list[dict[str, object]]: ...


