from __future__ import annotations

import json
import ssl
from datetime import datetime
from typing import Annotated, Literal
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from al1s.execution.errors import ExecutionDomainError
from al1s.infrastructure.direct_http import NoRedirect


class HostManagerConnection(BaseModel):
    url: str
    token: SecretStr = Field(min_length=32)
    ca_file: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if any(character.isspace() for character in value) or chr(92) in value:
            raise ValueError("host manager origin must not contain whitespace or backslashes")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("host manager requires a credential-free HTTPS origin")
        _ = parsed.port
        return value.rstrip("/")


class MaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    action: Literal["restart_container", "restart_host", "upgrade_container"]
    expires_at: float = Field(allow_inf_nan=False, gt=0, le=253402300799)
    confirm_interrupt: Literal[True]
    expected_boot_id: str = Field(min_length=1, max_length=64)
    expected_container_id: str = Field(max_length=64)
    expected_container_started_at: str = Field(max_length=64)
    confirmed_upgrade_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
    )


class RestartRequest(MaintenanceRequest):
    action: Literal["restart_container", "restart_host"]


class UpgradeRequest(MaintenanceRequest):
    action: Literal["upgrade_container"]
    release_id: UUID


class HostManagerClient:
    def __init__(self, connection: HostManagerConnection):
        self.connection = connection

    def request(self, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        request = Request(
            self.connection.url + path,
            method="POST" if body else "GET",
            data=json.dumps(body).encode() if body else None,
            headers={
                "Authorization": "Bearer " + self.connection.token.get_secret_value(),
                "Content-Type": "application/json",
            },
        )
        try:
            context = ssl.create_default_context(cafile=self.connection.ca_file)
            opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
            timeout = 75 if path == "/v1/upgrades/recover" else 15
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(16385)
                if len(raw) > 16384:
                    raise ValueError("oversized_response")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("invalid_response")
                if path == "/v1/health":
                    return HostHealth.model_validate(value).model_dump(mode="json")
                if path == "/v1/logs":
                    return HostLogs.model_validate(value).model_dump(mode="json")
                if path == "/v1/upgrades/recover":
                    result = RecoveryResult.model_validate(value)
                    if not body or result.deployment_id != body.get("deployment_id"):
                        raise ValueError("deployment_identity_mismatch")
                    return result.model_dump(mode="json")
                command = HostCommand.model_validate(value)
                expected = body.get("command_id") if body else path.rsplit("/", 1)[-1]
                if str(command.command_id) != str(expected):
                    raise ValueError("command_identity_mismatch")
                return command.model_dump(mode="json")
        except HTTPError as exc:
            exc.close()
            status = exc.code if exc.code in {404, 409} else 502
            raise ExecutionDomainError(
                "host_management_rejected",
                "Host command rejected; refresh status before retry",
                status,
            ) from exc
        except (OSError, ValueError) as exc:
            raise ExecutionDomainError(
                "host_management_unreachable_or_unknown",
                "Management channel unavailable; submitted command result may be unknown",
                503,
            ) from exc


class HostHealth(BaseModel):
    boot_id: str = Field(min_length=1, max_length=64)
    container_id: str = Field(max_length=64)
    container_started_at: str = Field(max_length=64)
    healthy: bool
    upgrade_in_progress: bool = False
    unresolved_upgrade_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
    )


class HostLogs(BaseModel):
    observed_at: datetime
    lines: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=100)


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deployment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class CancelMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    version: int = Field(ge=1)


class RecoveryResult(RecoveryRequest):
    resolved: bool
    status: Literal["succeeded", "failed", "rolled_back"] | None = None
    reason: str | None = Field(default=None, max_length=100)


class HostCommand(BaseModel):
    command_id: UUID
    action: Literal["restart_container", "restart_host", "upgrade_container"]
    state: Literal[
        "accepted",
        "executing",
        "recovering",
        "succeeded",
        "expired",
        "refused",
        "recovery_timeout",
        "failed",
        "cancelled",
    ]
    accepted_at: float = Field(allow_inf_nan=False, ge=0, le=253402300799)
    started_at: float | None = Field(allow_inf_nan=False, ge=0, le=253402300799)
    expires_at: float = Field(allow_inf_nan=False, gt=0, le=253402300799)
    error_code: str | None = Field(max_length=64)
    version: int = Field(ge=1)
    late_state: Literal["succeeded", "failed", "cancelled"] | None = None
    release_id: UUID | None = None
