from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.notifications.discord_forward_rules import DiscordForwardRuleService
from al1s.notifications.discord_forward_types import ActionInput, ChannelState, RuleInput
from al1s.notifications.errors import NotificationDomainError


def rule(action: ActionInput) -> RuleInput:
    return RuleInput(
        bot_service_id=uuid4(),
        name="rule",
        guild_id="123456789012345678",
        channel_id="223456789012345678",
        trigger_kind="contains",
        trigger_text="hello",
        frequency_count=None,
        frequency_window_seconds=None,
        cooldown_seconds=None,
        enabled=True,
        actions=(action,),
    )


def test_invalid_input_never_reaches_repository() -> None:
    service = DiscordForwardRuleService(Mock())
    service._repository = Mock()
    with pytest.raises(NotificationDomainError, match="Invalid Discord source"):
        service.save(
            replace(rule(ActionInput(uuid4(), "qq_private", "12345", "none")), guild_id="bad")
        )
    service._repository.save.assert_not_called()


def test_action_validation_uses_batch_loaded_channel_state() -> None:
    service = DiscordForwardRuleService(Mock())
    channel_id = uuid4()
    action = ActionInput(channel_id, "qq_group", "12345", "lexicon")
    service._validate_actions({channel_id: ChannelState("qq", True, False)}, (action,))
    with pytest.raises(NotificationDomainError, match="Unavailable destination channel"):
        service._validate_actions({channel_id: ChannelState("qq", False, False)}, (action,))
