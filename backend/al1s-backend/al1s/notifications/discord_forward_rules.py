"""Business validation and orchestration for Discord forwarding rules."""

from __future__ import annotations

import re
from uuid import UUID

from al1s.notifications.discord_forward_types import (
    ActionInput,
    ChannelState,
    DiscordRuleRepository,
    RuleInput,
)
from al1s.notifications.errors import NotificationDomainError

SNOWFLAKE = re.compile(r"^[1-9][0-9]{16,19}$")
QQ_ID = re.compile(r"^[1-9][0-9]{4,11}$")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class DiscordForwardRuleService:
    def __init__(self, repository: DiscordRuleRepository):
        self._repository = repository

    def list_page(
        self, bot_service_id: UUID, *, after_id: UUID | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        return self._repository.list_page(bot_service_id, after_id=after_id, limit=limit)

    def enabled_for_worker(self, bot_service_id: UUID) -> list[dict[str, object]]:
        return self._repository.enabled_for_worker(bot_service_id)

    def save(
        self, data: RuleInput, *, rule_id: UUID | None = None, expected_version: int | None = None
    ) -> dict[str, object]:
        self._validate_input(data)
        return self._repository.save(
            data,
            rule_id=rule_id,
            expected_version=expected_version,
            validate_actions=self._validate_actions,
        )

    def delete(self, rule_id: UUID, expected_version: int) -> None:
        self._repository.delete(rule_id, expected_version)

    def match_actions(
        self, bot_service_id: UUID, rule_id: UUID, version: int, guild_id: str, channel_id: str
    ) -> list[dict[str, object]]:
        return self._repository.match_actions(
            bot_service_id, rule_id, version, guild_id, channel_id
        )

    @staticmethod
    def _validate_input(data: RuleInput) -> None:
        if not data.name.strip() or len(data.name.strip()) > 100:
            raise NotificationDomainError("invalid_rule_name", "Invalid rule name")
        if not SNOWFLAKE.fullmatch(data.guild_id) or not SNOWFLAKE.fullmatch(data.channel_id):
            raise NotificationDomainError("invalid_discord_source", "Invalid Discord source")
        if not 1 <= len(data.actions) <= 20:
            raise NotificationDomainError("invalid_rule_actions", "Rule needs 1-20 actions")
        if data.trigger_kind == "contains" and not (
            data.trigger_text and data.trigger_text.strip() and 1 <= len(data.trigger_text) <= 256
        ):
            raise NotificationDomainError("invalid_trigger_text", "Invalid trigger text")
        if data.trigger_kind == "acrostic" and not (
            data.trigger_text and data.trigger_text.strip() and 1 <= len(data.trigger_text) <= 64
        ):
            raise NotificationDomainError("invalid_acrostic", "Acrostic must have 1-64 characters")
        if data.trigger_kind == "frequency" and not (
            data.frequency_count is not None
            and 2 <= data.frequency_count <= 64
            and data.frequency_window_seconds is not None
            and 1 <= data.frequency_window_seconds <= 3600
            and data.cooldown_seconds is not None
            and 0 <= data.cooldown_seconds <= 3600
        ):
            raise NotificationDomainError("invalid_frequency", "Invalid frequency window")
        if data.trigger_kind not in {"contains", "acrostic", "frequency"}:
            raise NotificationDomainError("invalid_trigger_kind", "Invalid trigger kind")

    @staticmethod
    def _validate_actions(
        channels: dict[UUID, ChannelState], actions: tuple[ActionInput, ...]
    ) -> None:
        for action in actions:
            if action.kind not in {"qq_private", "qq_group", "smtp"}:
                raise NotificationDomainError("invalid_action_kind", "Invalid action kind")
            channel = channels.get(action.channel_id)
            expected = "smtp" if action.kind == "smtp" else "qq"
            if (
                channel is None
                or channel.deleted
                or not channel.enabled
                or channel.kind != expected
            ):
                raise NotificationDomainError(
                    "invalid_action_channel", "Unavailable destination channel"
                )
            if action.kind == "smtp":
                valid_target = bool(EMAIL.fullmatch(action.target))
            else:
                valid_target = bool(QQ_ID.fullmatch(action.target))
            if not valid_target:
                raise NotificationDomainError("invalid_action_target", "Invalid destination")
            if (
                action.review_policy not in {"none", "lexicon"}
                or (action.kind == "qq_group" and action.review_policy != "lexicon")
                or (action.kind == "smtp" and action.review_policy != "none")
            ):
                raise NotificationDomainError("invalid_action_policy", "Invalid review policy")
