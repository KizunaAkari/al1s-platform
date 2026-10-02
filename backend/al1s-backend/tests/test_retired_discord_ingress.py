import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.api.reviews import router
from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.review_service import ReviewService
from al1s.notifications.service import NotificationService
from al1s.notifications.types import NotificationKind
from al1s.secrets.security import FernetSecretCipher


def test_only_rule_match_worker_ingress_is_registered() -> None:
    paths = {route.path for route in router.routes}
    assert "/bots/worker/rule-matches" in paths
    assert "/bots/worker/messages" not in paths


def test_retired_forward_route_cannot_be_created_or_reenabled() -> None:
    cipher = FernetSecretCipher("test-master-key-" * 3)
    service = NotificationService(lambda: None, cipher)
    with pytest.raises(NotificationDomainError) as caught:
        service.create_route(
            notification_kind=NotificationKind.FORWARD, channel_id=uuid4(),
            targets=["private:123"], template_key="forward.v1", actor_id=None,
            correlation_id=uuid4(),
        )
    assert caught.value.code == "legacy_forward_retired"

    row = SimpleNamespace(
        notification_kind=NotificationKind.FORWARD, deleted_at=None, row_version=1
    )

    class UnitOfWork:
        routes = SimpleNamespace(get=lambda *_args, **_kwargs: row)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    service = NotificationService(UnitOfWork, cipher)
    with pytest.raises(NotificationDomainError) as caught:
        service.update_route(
            route_id=uuid4(), expected_version=1, targets=None, template_key=None,
            enabled=True, actor_id=None, correlation_id=uuid4(),
        )
    assert caught.value.code == "legacy_forward_retired"


def test_retired_review_cannot_create_a_new_delivery() -> None:
    cipher = FernetSecretCipher("test-master-key-" * 3)
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid4(), state="approved", row_version=1, finalized_at=None, intent_id=None,
        body_cipher=cipher.encrypt(json.dumps({
            "text": "historical message", "mention_everyone": True,
            "allow_private": True, "target_bot_id": str(uuid4()),
        })),
        key_id=cipher.key_id,
    )
    intents = Mock()
    deliveries = Mock()
    service = ReviewService(lambda: None, cipher, None)
    service._enqueue_many(SimpleNamespace(intents=intents, deliveries=deliveries), [row], now)

    assert intents.add_many.call_args.args[0][0].disposition == "no_route"
    deliveries.add_many.assert_called_once_with([])
    assert row.state == "completed"


def test_retired_pending_review_cannot_be_approved() -> None:
    cipher = FernetSecretCipher("test-master-key-" * 3)
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid4(), state="pending", row_version=1, received_at=now, pending_at=now,
        approved_at=None, finalized_at=None, deleted_at=None,
        body_cipher=cipher.encrypt(json.dumps({"text": "historical message"})),
        key_id=cipher.key_id,
    )

    class UnitOfWork:
        reviews = SimpleNamespace(get=lambda _identity: row)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    service = ReviewService(UnitOfWork, cipher, None)
    with pytest.raises(NotificationDomainError, match="Legacy forwarding review") as caught:
        service.approve(row.id, row.row_version, None)
    assert caught.value.code == "legacy_forward_retired"
    assert row.state == "pending"
