from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.adapters.mqtt.publisher import MqttOutboxPublisher
from al1s.kernel.types import ClaimedOutboxEvent


class FakePublishInfo:
    rc = 0

    def wait_for_publish(self, timeout: float) -> None:
        assert timeout > 0

    def is_published(self) -> bool:
        return True


class FakeMqttClient:
    def __init__(self) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.published: list[tuple[str, str, int, bool]] = []

    def username_pw_set(self, username: str, password: str) -> None:
        assert username and password

    def tls_set(self) -> None:
        return None

    def connect(self, host: str, port: int, keepalive: int) -> None:
        assert host and port and keepalive

    def loop_start(self) -> None:
        assert self.on_connect is not None
        self.on_connect(
            self,
            None,
            None,
            SimpleNamespace(is_failure=False),
            None,
        )

    def disconnect(self) -> None:
        return None

    def loop_stop(self) -> None:
        return None

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> FakePublishInfo:
        self.published.append((topic, payload, qos, retain))
        return FakePublishInfo()


def test_outbox_publisher_emits_general_event_and_terminal_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeMqttClient()
    monkeypatch.setattr(
        "al1s.adapters.mqtt.publisher.mqtt.Client",
        lambda **_kwargs: client,
    )
    publisher = MqttOutboxPublisher(
        host="mqtt.example.test",
        port=1883,
        username="admin",
        password="secret",
    )
    terminal_id = uuid4()
    event = ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type="task_package.available.v1",
        schema_version=1,
        aggregate_type="task_package",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=datetime(2026, 9, 1, 8, tzinfo=UTC),
        payload={"terminal_id": str(terminal_id), "package_id": str(uuid4())},
        attempt_count=1,
        row_version=2,
    )

    publisher.publish(event)
    publisher.close()

    assert [item[0] for item in client.published] == [
        "al1s/v1/events/task_package.available.v1",
        f"al1s/v1/terminals/{terminal_id}/hints",
    ]
    body = json.loads(client.published[1][1])
    assert body["event_id"] == str(event.event_id)
    assert body["payload"] == event.payload
    assert client.published[1][2:] == (1, False)
