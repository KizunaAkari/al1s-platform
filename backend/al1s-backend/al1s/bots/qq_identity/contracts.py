import re
import unicodedata
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QqIdentityError(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


class IdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class QqDeviceSettings(IdentityModel):
    device_name: str = Field(max_length=64)
    guid: str

    @field_validator("device_name")
    @classmethod
    def name(cls, value: str) -> str:
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
            raise ValueError("qq_device_name_invalid")
        return value.strip()

    @field_validator("guid")
    @classmethod
    def device_guid(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(
            r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
            value,
        ):
            raise ValueError("qq_device_guid_invalid")
        result = value.replace("-", "").lower()
        if result == "0" * 32:
            raise ValueError("qq_device_guid_invalid")
        return result


class QqIdentitySave(IdentityModel):
    settings: QqDeviceSettings
    confirm_guid_change: bool = False


class AppliedIdentity(IdentityModel):
    settings: QqDeviceSettings
    reported_device_name: str
    applied_at: datetime


class IdentityRecord(IdentityModel):
    schema_version: Literal[1] = 1
    version: int = Field(default=0, ge=0)
    configured: bool = False
    desired: QqDeviceSettings
    applied: AppliedIdentity | None = None
    source_guid_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: UUID | None = None
    request_sha256: str | None = None
    phase: str = "uninitialized"
    error_code: str | None = None


class QqIdentityState(IdentityModel):
    mode: Literal["direct", "pmhq"] = "direct"
    available: bool = True
    unavailable_reason: str | None = None
    revision: str
    configured: bool
    desired: QqDeviceSettings
    applied: AppliedIdentity | None
    current_guid: str
    pending: bool
    phase: str
    error_code: str | None
    observed_at: datetime


class QqIdentitySummary(IdentityModel):
    configured: bool = False
    pending: bool = False
    failed: bool = False
    custom_name: bool = False
    configuration_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
