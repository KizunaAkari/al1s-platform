"""Restricted Docker operations. Never return raw inspect data or execute a shell."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Alias = Literal["qq", "discord"]
Action = Literal["start", "stop", "restart"]


class ContainerState(BaseModel):
    alias: Alias
    instance_id: str
    running: bool
    status: str
    started_at: str
    finished_at: str
    health: str
    revision: str


class ContainerCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ControlError(Exception):
    def __init__(self, code: str, status: int = 503):
        self.code, self.status = code, status
        super().__init__(code)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=25)
        self.path = path

    def connect(self) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise OSError("unix_socket_unavailable")
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.settimeout(25)
        self.sock.connect(self.path)


class DockerControl:
    def __init__(self, targets: dict[str, str], socket_path: str, version: str = "1.45"):
        if not targets or set(targets) - {"qq", "discord"}:
            raise ValueError("invalid_control_aliases")
        if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", x)
               for x in targets.values()) or not re.fullmatch(r"1\.\d{2,3}", version):
            raise ValueError("invalid_control_configuration")
        if len(set(targets.values())) != len(targets):
            raise ValueError("duplicate_control_target")
        self.targets, self.socket_path, self.version = targets, socket_path, version
        self.locks = {alias: Lock() for alias in targets}

    def _request(self, method: str, target: str, suffix: str) -> dict[str, object]:
        connection = UnixConnection(self.socket_path)
        try:
            connection.request(method, f"/v{self.version}/containers/{target}/{suffix}")
            response = connection.getresponse()
            raw = response.read(1_048_577)
            if response.status == 404:
                raise ControlError("container_not_found", 404)
            if response.status not in {200, 204, 304}:
                raise ControlError("docker_operation_failed", 502)
            if len(raw) > 1_048_576:
                raise ControlError("docker_response_too_large", 502)
            value = json.loads(raw) if raw else {}
            if not isinstance(value, dict):
                raise ControlError("invalid_docker_response", 502)
            return value
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise ControlError("docker_unavailable_or_result_unknown") from exc
        finally:
            connection.close()

    def state(self, alias: Alias) -> ContainerState:
        if alias not in self.targets:
            raise ControlError("container_not_registered", 404)
        value = self._request("GET", self.targets[alias], "json")
        state = value.get("State")
        if not isinstance(state, dict) or not isinstance(state.get("Running"), bool):
            raise ControlError("invalid_docker_state", 502)
        identity = value.get("Id")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ControlError("invalid_docker_identity", 502)
        health = state.get("Health", {})
        fields = {
            "instance_id": identity, "running": state["Running"],
            "status": str(state.get("Status", "unknown")),
            "started_at": str(state.get("StartedAt", "")),
            "finished_at": str(state.get("FinishedAt", "")),
        }
        revision = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        return ContainerState(
            alias=alias, health=str(health.get("Status", "not_configured"))
            if isinstance(health, dict) else "unknown", revision=revision, **fields,
        )

    def command(self, alias: Alias, command: ContainerCommand) -> ContainerState:
        if alias not in self.locks:
            raise ControlError("container_not_registered", 404)
        if not self.locks[alias].acquire(blocking=False):
            raise ControlError("container_operation_in_progress", 409)
        try:
            before = self.state(alias)
            if before.revision != command.revision:
                raise ControlError("container_state_changed_refresh_required", 409)
            if command.action == "start" and before.running:
                return before
            if command.action == "stop" and not before.running:
                return before
            suffix = command.action + ("?t=10" if command.action != "start" else "")
            # Resolve name once, then mutate the immutable instance, never a replacement by name.
            self._request("POST", before.instance_id, suffix)
            after = self.state(alias)
            expected_running = command.action != "stop"
            if after.instance_id != before.instance_id or after.running != expected_running:
                raise ControlError("container_result_unconfirmed_refresh_required", 409)
            if command.action == "restart" and after.started_at == before.started_at:
                raise ControlError("container_restart_unconfirmed", 409)
            return after
        finally:
            self.locks[alias].release()
