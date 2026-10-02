from __future__ import annotations

import json
import threading
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from al1s.kernel.types import ClaimedOutboxEvent


class MqttPublishError(RuntimeError):
    pass


class MqttOutboxPublisher:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        client_id: str = "al1s-platform-publisher",
        timeout_seconds: float = 5.0,
        tls_enabled: bool = False,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._connected = threading.Event()
        self._connection_error: str | None = None
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
        )
        self._client.username_pw_set(username, password)
        if tls_enabled:
            self._client.tls_set()

        def on_connect(
            client: mqtt.Client,
            userdata: object,
            flags: mqtt.ConnectFlags,
            reason_code: ReasonCode,
            properties: Properties | None,
        ) -> None:
            del client, userdata, flags, properties
            if reason_code.is_failure:
                self._connection_error = str(reason_code)
            else:
                self._connection_error = None
            self._connected.set()

        def on_disconnect(
            client: mqtt.Client,
            userdata: object,
            disconnect_flags: mqtt.DisconnectFlags,
            reason_code: ReasonCode,
            properties: Properties | None,
        ) -> None:
            del client, userdata, disconnect_flags, reason_code, properties
            self._connected.clear()

        self._client.on_connect = on_connect
        self._client.on_disconnect = on_disconnect
        self._client.connect(host, port, keepalive=30)
        self._client.loop_start()
        if not self._connected.wait(timeout_seconds) or self._connection_error is not None:
            self.close()
            raise MqttPublishError(
                f"MQTT publisher connection failed: {self._connection_error or 'timeout'}"
            )

    def publish(self, event: ClaimedOutboxEvent) -> None:
        body = _event_body(event)
        self._publish(f"al1s/v1/events/{event.event_type}", body)
        terminal_id = event.payload.get("terminal_id")
        if event.event_type in {
            "task_package.available.v1",
            "execution.cancel_requested.v1",
        } and isinstance(terminal_id, str):
            self._publish(f"al1s/v1/terminals/{terminal_id}/hints", body)

    def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def _publish(self, topic: str, body: dict[str, Any]) -> None:
        if not self._connected.wait(self._timeout_seconds):
            raise MqttPublishError("MQTT publisher is disconnected")
        info = self._client.publish(
            topic,
            json.dumps(body, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttPublishError(f"MQTT publish failed: {info.rc}")
        info.wait_for_publish(timeout=self._timeout_seconds)
        if not info.is_published():
            raise MqttPublishError("MQTT publish acknowledgement timed out")


def _event_body(event: ClaimedOutboxEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "correlation_id": str(event.correlation_id),
        "occurred_at": event.occurred_at.isoformat(),
        "payload": event.payload,
    }
