from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from al1s.adapters.mqtt.mosquitto import MosquittoDynamicSecurityBroker
from al1s.adapters.mqtt.publisher import MqttOutboxPublisher
from al1s.execution.mqtt_sessions import (
    MqttSessionPasswordSigner,
    MqttSessionStatus,
    TerminalMqttSessionRecord,
)
from al1s.kernel.types import ClaimedOutboxEvent

pytestmark = pytest.mark.contract


def test_dynamic_security_terminal_hint_round_trip() -> None:
    if os.getenv("AL1S_TEST_MQTT") != "1":
        pytest.skip("AL1S_TEST_MQTT=1 is required for MQTT contract tests")
    host = os.getenv("AL1S_TEST_MQTT_HOST", "127.0.0.1")
    port = int(os.getenv("AL1S_TEST_MQTT_PORT", "18884"))
    admin_username = os.getenv("AL1S_TEST_MQTT_USERNAME", "al1s-test-admin")
    admin_password = os.getenv("AL1S_TEST_MQTT_PASSWORD", "al1s-test-password")
    now = datetime.now(UTC)
    terminal_id = uuid4()
    session = TerminalMqttSessionRecord(
        session_id=uuid4(),
        terminal_id=terminal_id,
        client_id=f"contract-client-{uuid4().hex}",
        username=f"contract-user-{uuid4().hex}",
        role_name=f"contract-role-{uuid4().hex}",
        topic=f"al1s/v1/terminals/{terminal_id}/hints",
        password_digest="",
        status=MqttSessionStatus.PENDING,
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
        configured_at=None,
        revoked_at=None,
        row_version=1,
    )
    signer = MqttSessionPasswordSigner("contract-mqtt-signing-key-at-least-32-bytes")
    password = signer.password(session)
    broker = MosquittoDynamicSecurityBroker(
        host=host,
        port=port,
        admin_username=admin_username,
        admin_password=admin_password,
    )
    broker.ensure_terminal_subscriber(session, password)
    broker.ensure_terminal_subscriber(session, password)
    publisher_username = f"contract-publisher-{uuid4().hex}"
    publisher_password = f"contract-publisher-password-{uuid4().hex}"
    publisher_client_id = f"contract-publisher-client-{uuid4().hex}"
    publisher_role_name = f"contract-publisher-role-{uuid4().hex}"
    broker.ensure_platform_publisher(
        username=publisher_username,
        password=publisher_password,
        client_id=publisher_client_id,
        role_name=publisher_role_name,
    )
    broker.ensure_platform_publisher(
        username=publisher_username,
        password=publisher_password,
        client_id=publisher_client_id,
        role_name=publisher_role_name,
    )
    received = threading.Event()
    connected = threading.Event()
    subscriber = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=session.client_id,
        protocol=mqtt.MQTTv5,
    )
    subscriber.username_pw_set(session.username, password)

    def on_connect(
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        del userdata, flags, properties
        assert not reason_code.is_failure
        client.subscribe(session.topic, qos=1)
        connected.set()

    def on_message(
        client: mqtt.Client,
        userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        del client, userdata
        if message.topic == session.topic:
            received.set()

    subscriber.on_connect = on_connect
    subscriber.on_message = on_message
    publisher: MqttOutboxPublisher | None = None
    try:
        subscriber.connect(host, port, keepalive=15)
        subscriber.loop_start()
        assert connected.wait(5)
        publisher = MqttOutboxPublisher(
            host=host,
            port=port,
            username=publisher_username,
            password=publisher_password,
            client_id=publisher_client_id,
        )
        publisher.publish(
            ClaimedOutboxEvent(
                event_id=uuid4(),
                event_type="task_package.available.v1",
                schema_version=1,
                aggregate_type="task_package",
                aggregate_id=uuid4(),
                correlation_id=uuid4(),
                occurred_at=now,
                payload={"terminal_id": str(terminal_id)},
                attempt_count=1,
                row_version=2,
            )
        )
        assert received.wait(5)
    finally:
        if publisher is not None:
            publisher.close()
        subscriber.disconnect()
        subscriber.loop_stop()
        broker.revoke_terminal_subscriber(session)
