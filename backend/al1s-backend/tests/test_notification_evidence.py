from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.execution.notification_evidence import (
    EvidencePending,
    ExecutionNotificationEvidence,
    NotificationEvidence,
)
from al1s.notifications.dispatcher import NotificationDispatcher
from al1s.notifications.errors import NotificationAdapterError
from al1s.notifications.types import ChannelKind, NotificationKind
from tests.notification_support import setup_adapter


def payload():
    return {
        key: str(uuid4())
        for key in ("task_id", "execution_id", "attempt_id", "terminal_id", "capture_id")
    }


def test_pending_evidence_never_fetches_object():
    reader, store = Mock(), Mock()
    reader.read.return_value = NotificationEvidence("pending")
    with pytest.raises(EvidencePending):
        ExecutionNotificationEvidence(reader, store).read(payload())
    store.get_range.assert_not_called()


def test_object_read_is_bounded_hashed_and_permission_rechecked():
    body = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    ready = NotificationEvidence("ready", "controlled/key", len(body), sha256(body).hexdigest())
    reader, store = Mock(), Mock()
    reader.read.return_value = ready
    store.get_range.return_value = body
    service = ExecutionNotificationEvidence(reader, store)
    assert service.read(payload()) == body
    assert reader.read.call_count == 2
    reader.read.side_effect = [ready, NotificationEvidence("unavailable")]
    assert service.read(payload()) is None
    reader.read.side_effect = None
    store.get_range.return_value = b"bad"
    with pytest.raises(ValueError):
        service.read(payload())


def test_dispatch_defers_without_calling_provider_or_copying_image_to_payload():
    adapter = Mock()
    evidence = Mock(side_effect=EvidencePending())
    dispatcher = NotificationDispatcher(
        uow_factory=Mock(),
        adapters={ChannelKind.QQ: adapter},
        cipher=Mock(),
        worker_id="test",
        execution_evidence=evidence,
    )
    delivery = SimpleNamespace(
        channel_kind=ChannelKind.QQ,
        encrypted_secret=None,
        secret_key_id=None,
        template_key="conditional_skip.v1",
        payload=payload(),
        delivery_id=uuid4(),
        row_version=2,
        attempt_count=1,
        started_at=None,
    )
    result = dispatcher._send(delivery)
    assert result.evidence_pending
    adapter.send.assert_not_called()


def test_onebot_uses_only_configured_target_and_ephemeral_image_segment():
    adapter, message, post = setup_adapter("group:123")
    message.kind = NotificationKind.CONDITIONAL_SKIP
    message.image_png = b"\x89PNG\r\n\x1a\nimage"
    adapter.send(message, idempotency_key="test")
    sent = post.call_args.args[2]
    assert sent["group_id"] == 123
    assert sent["message"][1]["type"] == "image"
    assert sent["message"][1]["data"]["file"].startswith("base64://")


def test_image_cannot_be_attached_to_unrelated_notification():
    adapter, message, post = setup_adapter()
    message.kind = NotificationKind.TEST
    message.image_png = b"\x89PNG\r\n\x1a\nimage"
    with pytest.raises(NotificationAdapterError, match="onebot_invalid_image"):
        adapter.send(message, idempotency_key="test")
    post.assert_not_called()
