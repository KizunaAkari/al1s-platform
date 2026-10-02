from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from al1s.execution.mqtt_sessions import (
    MqttSecurityBroker,
    TerminalMqttSessionRecord,
)

_CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
_RESPONSE_TOPIC = f"{_CONTROL_TOPIC}/response"


class MosquittoDynamicSecurityError(RuntimeError):
    pass


class MosquittoDynamicSecurityBroker(MqttSecurityBroker):
    """Blocking, bounded adapter for Mosquitto Dynamic Security commands."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        admin_username: str,
        admin_password: str,
        timeout_seconds: float = 5.0,
        tls_enabled: bool = False,
    ) -> None:
        if not admin_username or not admin_password:
            raise ValueError("MQTT Dynamic Security admin credentials are required")
        self._host = host
        self._port = port
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._timeout_seconds = timeout_seconds
        self._tls_enabled = tls_enabled
        self._lock = threading.Lock()

    def ensure_terminal_subscriber(
        self, session: TerminalMqttSessionRecord, password: str
    ) -> None:
        commands: tuple[dict[str, object], ...] = (
            {"command": "createRole", "rolename": session.role_name},
            _role_acl(session, "subscribeLiteral"),
            _role_acl(session, "unsubscribeLiteral"),
            _role_acl(session, "publishClientReceive"),
            {
                "command": "createClient",
                "username": session.username,
                "password": password,
                "clientid": session.client_id,
            },
            {
                "command": "setClientPassword",
                "username": session.username,
                "password": password,
            },
            {
                "command": "setClientId",
                "username": session.username,
                "clientid": session.client_id,
            },
            {"command": "enableClient", "username": session.username},
        )
        self._execute(commands, allowed_errors=("already exists", "already has"))
        self._ensure_client_role(session.username, session.role_name)

    def revoke_terminal_subscriber(self, session: TerminalMqttSessionRecord) -> None:
        commands: tuple[dict[str, object], ...] = (
            {"command": "deleteClient", "username": session.username},
            {"command": "deleteRole", "rolename": session.role_name},
        )
        self._execute(commands, allowed_errors=("not found", "does not exist"))

    def ensure_platform_publisher(
        self,
        *,
        username: str,
        password: str,
        client_id: str = "al1s-platform-publisher",
        role_name: str = "al1s-platform-publisher",
    ) -> None:
        commands: tuple[dict[str, object], ...] = (
            {"command": "createRole", "rolename": role_name},
            {
                "command": "addRoleACL",
                "rolename": role_name,
                "acltype": "publishClientSend",
                "topic": "al1s/v1/events/#",
                "allow": True,
                "priority": 1,
            },
            {
                "command": "addRoleACL",
                "rolename": role_name,
                "acltype": "publishClientSend",
                "topic": "al1s/v1/terminals/+/hints",
                "allow": True,
                "priority": 1,
            },
            {
                "command": "createClient",
                "username": username,
                "password": password,
                "clientid": client_id,
            },
            {
                "command": "setClientPassword",
                "username": username,
                "password": password,
            },
            {
                "command": "setClientId",
                "username": username,
                "clientid": client_id,
            },
            {"command": "enableClient", "username": username},
        )
        self._execute(commands, allowed_errors=("already exists", "already has"))
        self._ensure_client_role(username, role_name)

    def _ensure_client_role(self, username: str, role_name: str) -> None:
        command: dict[str, object] = {
            "command": "addClientRole",
            "username": username,
            "rolename": role_name,
            "priority": 1,
        }
        try:
            self._execute((command,), allowed_errors=("already has",))
        except MosquittoDynamicSecurityError as exc:
            # Mosquitto 2.0 reports a duplicate client-role assignment as the
            # non-specific "Internal error". Verify broker state before
            # treating only that response as an idempotent success.
            if str(exc) != "addClientRole: Internal error" or not self._client_has_role(
                username, role_name
            ):
                raise

    def _client_has_role(self, username: str, role_name: str) -> bool:
        with self._lock:
            response = self._round_trip(({"command": "getClient", "username": username},))
        responses = response.get("responses")
        if not isinstance(responses, list) or len(responses) != 1:
            return False
        item = responses[0]
        if not isinstance(item, dict) or item.get("error"):
            return False
        data = item.get("data")
        client = data.get("client") if isinstance(data, dict) else None
        roles = client.get("roles") if isinstance(client, dict) else None
        return isinstance(roles, list) and any(
            isinstance(role, dict) and role.get("rolename") == role_name for role in roles
        )

    def _execute(
        self,
        commands: Sequence[dict[str, object]],
        *,
        allowed_errors: tuple[str, ...],
    ) -> None:
        with self._lock:
            response = self._round_trip(commands)
        responses = response.get("responses")
        if not isinstance(responses, list):
            raise MosquittoDynamicSecurityError("dynamic security response is invalid")
        errors: list[str] = []
        for item in responses:
            if not isinstance(item, dict):
                errors.append("invalid response item")
                continue
            error = item.get("error")
            if not isinstance(error, str) or not error:
                continue
            normalized = error.casefold()
            if any(allowed.casefold() in normalized for allowed in allowed_errors):
                continue
            command = item.get("command", "unknown")
            errors.append(f"{command}: {error}")
        if errors:
            raise MosquittoDynamicSecurityError("; ".join(errors))

    def _round_trip(self, commands: Sequence[dict[str, object]]) -> dict[str, Any]:
        correlation = uuid4().hex
        completed = threading.Event()
        payload: dict[str, Any] = {}
        failure: list[str] = []
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"al1s-dynsec-{uuid4().hex}",
            protocol=mqtt.MQTTv5,
        )
        client.username_pw_set(self._admin_username, self._admin_password)
        if self._tls_enabled:
            client.tls_set()

        def on_connect(
            connected: mqtt.Client,
            userdata: object,
            flags: mqtt.ConnectFlags,
            reason_code: ReasonCode,
            properties: Properties | None,
        ) -> None:
            del userdata, flags, properties
            if reason_code.is_failure:
                failure.append(f"MQTT connect failed: {reason_code}")
                completed.set()
                return
            connected.subscribe(_RESPONSE_TOPIC, qos=1)

        def on_subscribe(
            connected: mqtt.Client,
            userdata: object,
            mid: int,
            reason_code_list: list[ReasonCode],
            properties: Properties | None,
        ) -> None:
            del userdata, mid, properties
            if any(reason.is_failure for reason in reason_code_list):
                failure.append("MQTT Dynamic Security response subscription was rejected")
                completed.set()
                return
            body = {
                "commands": [dict(command, correlationData=correlation) for command in commands]
            }
            info = connected.publish(_CONTROL_TOPIC, json.dumps(body), qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                failure.append(f"MQTT Dynamic Security publish failed: {info.rc}")
                completed.set()

        def on_message(
            connected: mqtt.Client,
            userdata: object,
            message: mqtt.MQTTMessage,
        ) -> None:
            del connected, userdata
            try:
                candidate = json.loads(message.payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(candidate, dict):
                return
            responses = candidate.get("responses")
            if not isinstance(responses, list):
                return
            if not any(
                isinstance(item, dict) and item.get("correlationData") == correlation
                for item in responses
            ):
                return
            payload.update(candidate)
            completed.set()

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        try:
            client.connect(self._host, self._port, keepalive=15)
            client.loop_start()
            if not completed.wait(self._timeout_seconds):
                raise MosquittoDynamicSecurityError("dynamic security command timed out")
            if failure:
                raise MosquittoDynamicSecurityError(failure[0])
            return payload
        finally:
            client.disconnect()
            client.loop_stop()


def _role_acl(session: TerminalMqttSessionRecord, acl_type: str) -> dict[str, object]:
    return {
        "command": "addRoleACL",
        "rolename": session.role_name,
        "acltype": acl_type,
        "topic": session.topic,
        "allow": True,
        "priority": 1,
    }
