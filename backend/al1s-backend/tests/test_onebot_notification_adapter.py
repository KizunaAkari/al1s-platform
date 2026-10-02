from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.bots.runtime_connection import BotConnectionReader
from al1s.bots.types import BotServiceKind
from al1s.notifications.errors import NotificationAdapterError
from tests.notification_support import setup_adapter


@pytest.mark.parametrize(
    "target,action,key",
    [
        ("private:123", "send_private_msg", "user_id"),
        ("group:123", "send_group_msg", "group_id"),
    ],
)
def test_single_target_single_attempt(target, action, key):
    adapter, message, post = setup_adapter(target)
    assert adapter.send(message, idempotency_key="delivery").provider_message_id == "-12"
    post.assert_called_once_with(
        f"http://llbot:3000/{action}",
        "secret",
        {
            key: 123,
            "message": [{"type": "text", "data": {"text": "hello"}}],
        },
    )


@pytest.mark.parametrize("target", ["123", "group:0", "private:9007199254740992", "group:123/x"])
def test_invalid_target_never_sends(target):
    adapter, message, post = setup_adapter(target)
    with pytest.raises(NotificationAdapterError):
        adapter.send(message, idempotency_key="delivery")
    post.assert_not_called()


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"status": "ok", "retcode": False},
        {"status": "ok", "retcode": "0"},
        {"status": "failed", "retcode": 100, "wording": "sensitive body"},
        {"status": "ok", "retcode": 0, "data": {"message_id": True}},
    ],
)
def test_invalid_receipt_is_not_success_or_leaked(result):
    adapter, message, post = setup_adapter()
    post.return_value = result
    with pytest.raises(NotificationAdapterError) as error:
        adapter.send(message, idempotency_key="delivery")
    assert "sensitive" not in str(error.value)
    assert not error.value.retryable
    assert post.call_count == 1


def test_reads_applied_not_desired_and_closes_transaction():
    uow = MagicMock()
    uow.__enter__.return_value = uow
    service_id, applied = uuid4(), uuid4()
    uow.services.get.return_value = SimpleNamespace(
        enabled=True,
        deleted_at=None,
        kind=BotServiceKind.ONEBOT_GATEWAY,
        applied_config_version_id=applied,
        desired_config_version_id=uuid4(),
    )
    uow.configs.get_version.return_value = SimpleNamespace(
        service_id=service_id,
        settings={"ONEBOT_BASE_URL": "http://llbot:3000"},
        secret_id=None,
    )
    result = BotConnectionReader(lambda: uow, MagicMock()).read(
        service_id,
        BotServiceKind.ONEBOT_GATEWAY,
    )
    uow.configs.get_version.assert_called_once_with(applied)
    uow.__exit__.assert_called_once()
    assert result.secret is None
