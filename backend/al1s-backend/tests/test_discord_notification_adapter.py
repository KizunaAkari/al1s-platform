from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.adapters.notifications.discord import DiscordNotificationAdapter
from al1s.notifications.errors import NotificationAdapterError


def setup_adapter():
    connections = MagicMock()
    connections.read.return_value = SimpleNamespace(secret="test-token")
    post = MagicMock(return_value={"id": "123456789012345678"})
    message = SimpleNamespace(
        bot_service_id=uuid4(), targets=("channel:223456789012345678",), body="@everyone test"
    )
    return DiscordNotificationAdapter(connections, post), message, post


def test_single_http_send_disables_mentions_and_keeps_nonce_stable():
    adapter, message, post = setup_adapter()
    assert adapter.send(message, idempotency_key="same").provider_message_id == "123456789012345678"
    payload = post.call_args.args[2]
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["enforce_nonce"] is True and len(payload["nonce"]) <= 25
    post.assert_called_once()
    adapter.send(message, idempotency_key="same")
    assert post.call_args.args[2] == payload


@pytest.mark.parametrize("target", ["123456789012345678", "channel:0", "channel:../x"])
def test_invalid_target_cannot_make_network_request(target):
    adapter, message, post = setup_adapter()
    message.targets = (target,)
    with pytest.raises(NotificationAdapterError):
        adapter.send(message, idempotency_key="test")
    post.assert_not_called()


def test_oversized_content_is_not_silently_truncated():
    adapter, message, post = setup_adapter()
    message.body = "x" * 2001
    with pytest.raises(NotificationAdapterError):
        adapter.send(message, idempotency_key="test")
    post.assert_not_called()
