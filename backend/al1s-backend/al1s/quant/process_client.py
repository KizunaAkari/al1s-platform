"""Connect to the quant process only through its protected Unix socket."""

import http.client
import json
import socket
from collections.abc import Callable
from uuid import UUID

from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.quant.commands import QuantCommand
from al1s.quant.messages import QuantCommandRequest

Request = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class QuantProcessClient:
    def __init__(self, socket_path: str, control_token: str, *, request: Request | None = None):
        if not socket_path.startswith("/") or len(control_token.encode()) < 32:
            raise ValueError("quant_process_configuration_invalid")
        self._path = socket_path
        self._request = request or self._send
        self._headers = {"X-Quant-Control": control_token, "Content-Type": "application/json"}

    def _send(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        connection = _UnixConnection(
            self._path, 150 if path.startswith("/internal/command/") else 3
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError("quant_process_response_invalid")
            return response.status, data
        finally:
            connection.close()

    @staticmethod
    def _versions(response: tuple[int, bytes]) -> tuple[int, int]:
        status, body = response
        if status != 200:
            raise ValueError("quant_process_unavailable")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("quant_process_response_invalid") from error
        if (
            not isinstance(data, dict)
            or set(data) != {"row_version", "mapping_version"}
            or type(data["row_version"]) is not int
            or type(data["mapping_version"]) is not int
            or data["row_version"] < 0
            or data["mapping_version"] < 0
        ):
            raise ValueError("quant_process_response_invalid")
        return data["row_version"], data["mapping_version"]

    def activate(self, service_id: UUID, desired: QuantSettingsRecord) -> tuple[int, int]:
        body = desired.model_dump_json().encode()
        return self._versions(
            self._request("PUT", f"/internal/config/{service_id}", body, self._headers)
        )

    def applied_versions(self, service_id: UUID) -> tuple[int, int]:
        return self._versions(
            self._request("GET", f"/internal/config/{service_id}", None, self._headers)
        )

    def execute(self, service_id: UUID, command: QuantCommand) -> str:
        request = QuantCommandRequest(
            source=command.source,
            action=command.action,
            arguments=list(command.arguments),
            mapping_version=command.mapping_version,
            settings_version=command.settings_version,
            event_timestamp=command.event_timestamp,
            normalized_keyword=command.normalized_keyword,
            settings=command.settings,
            commands=dict(command.command_words),
        )
        status, body = self._request(
            "POST",
            f"/internal/command/{service_id}",
            request.model_dump_json().encode(),
            self._headers,
        )
        if status != 200:
            raise ValueError("quant_process_unavailable")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("quant_process_response_invalid") from error
        if (
            not isinstance(data, dict)
            or set(data) != {"content"}
            or not isinstance(data["content"], str)
            or not 1 <= len(data["content"]) <= 4096
        ):
            raise ValueError("quant_process_response_invalid")
        return data["content"]

    def close(self) -> None:
        pass

    def status(self) -> dict:
        status, body = self._request("GET", "/internal/health", None, self._headers)
        if status != 200:
            raise ValueError("quant_process_unavailable")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("quant_process_response_invalid") from error
        if not isinstance(data, dict) or set(data) != {
            "database",
            "source",
            "engine",
            "rss_bytes",
            "cpu_seconds",
            "cpu_percent",
        }:
            raise ValueError("quant_process_response_invalid")
        return data
