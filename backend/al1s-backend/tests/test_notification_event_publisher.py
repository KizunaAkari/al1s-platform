from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.kernel.types import ClaimedOutboxEvent
from al1s.notifications.event_publisher import NotificationEventPublisher
from al1s.notifications.types import NotificationKind


@pytest.mark.parametrize(
    "code, expected", [("archive_import_timeout", True), ("archive_import_cancelled", False)]
)
def test_import_failure_notification_does_not_copy_archive_content(code, expected):
    downstream, service = Mock(), Mock()
    event = ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type="maa.archive-import.failed.v1",
        schema_version=1,
        aggregate_type="maa_import_batch",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=datetime.now(UTC),
        attempt_count=1,
        row_version=1,
        payload={"error_code": code, "text": "secret archive body"},
    )
    NotificationEventPublisher(downstream, service).publish(event)
    downstream.publish.assert_called_once_with(event)
    assert service.materialize_intent.call_count == int(expected)
    if expected:
        arguments = service.materialize_intent.call_args.kwargs
        assert arguments["notification_kind"] is NotificationKind.OPERATION_FAILURE
        assert arguments["source_event_id"] == event.event_id
        assert "secret" not in str(arguments["payload"])


@pytest.mark.parametrize(
    "condition, expected",
    [
        ({"mode": "image", "threshold": 0.85}, "匹配阈值0.85"),
        ({"mode": "numeric", "operator": "gt", "value": 12}, "识别数值大于12"),
    ],
)
def test_skip_intent_contains_condition_target_and_reference_only(condition, expected):
    downstream, service = Mock(), Mock()
    task, capture, terminal, attempt = (str(uuid4()) for _ in range(4))
    event = ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type="execution.conditional_skip.v1",
        schema_version=1,
        aggregate_type="execution",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=datetime.now(UTC),
        attempt_count=1,
        row_version=1,
        payload={
            **condition,
            "task_id": task,
            "capture_id": capture,
            "terminal_id": terminal,
            "attempt_id": attempt,
            "target_step_number": 6,
            "data_base64": "not copied",
        },
    )
    NotificationEventPublisher(downstream, service).publish(event)
    result = service.materialize_intent.call_args.kwargs["payload"]
    assert expected in result["summary"] and "6" in result["summary"]
    assert result["capture_id"] == capture and result["task_id"] == task
    assert "data_base64" not in result


@pytest.mark.parametrize(
    "event_type,result,expected",
    [
        ("execution.ended.v1", "failure", True),
        ("execution.retry_queued.v1", "failure", True),
        ("execution.ended.v1", "success", False),
        ("execution.cancelled.v1", "failure", False),
        ("execution.stale.v1", "failure", False),
    ],
)
def test_only_accepted_failures_materialize(event_type, result, expected):
    downstream, service = Mock(), Mock()
    event = ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type=event_type,
        schema_version=1,
        aggregate_type="execution",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"result_kind": result},
        attempt_count=1,
        row_version=1,
    )
    NotificationEventPublisher(downstream, service).publish(event)
    downstream.publish.assert_called_once_with(event)
    assert service.materialize_intent.call_count == int(expected)
    if expected:
        arguments = service.materialize_intent.call_args.kwargs
        assert arguments["source_event_id"] == event.event_id
        assert arguments["notification_kind"] == NotificationKind.SCRIPT_FAILURE


def test_materialization_failure_is_not_acknowledged():
    downstream, service = Mock(), Mock()
    service.materialize_intent.side_effect = RuntimeError("database unavailable")
    event = ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type="execution.ended.v1",
        schema_version=1,
        aggregate_type="execution",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"result_kind": "failure"},
        attempt_count=1,
        row_version=1,
    )
    with pytest.raises(RuntimeError):
        NotificationEventPublisher(downstream, service).publish(event)
    downstream.publish.assert_not_called()
