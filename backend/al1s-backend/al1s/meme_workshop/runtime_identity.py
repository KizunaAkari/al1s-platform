import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.bots.qq_headed.contracts import QqProtocolMode


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["qq", "discord"]
    runtime_version: str = Field(pattern=r"^[0-9][A-Za-z0-9._+-]{0,63}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    native_version: str | None = Field(default=None, max_length=128)
    qq_device_identity_v1: bool = False
    qq_device_name_override: bool = False
    qq_protocol_mode: QqProtocolMode = "direct"
    qq_client_version: str | None = Field(default=None, min_length=1, max_length=64)
    pmhq_version: str | None = Field(default=None, min_length=1, max_length=64)
    qq_headed_v1: bool = False
    qq_quant_v1: bool = False
    qq_freqtrade_v1: bool = False
    qq_freqtrade_futures_v1: bool = False
    qq_freqtrade_visual_v1: bool = False
    qq_freqtrade_capital_v1: bool = False
    qq_command_replies_v1: bool = False
    qq_ai_v1: bool = False
    qq_ai_personas_v1: bool = False
    qq_ai_network_routes_v1: bool = False
    qq_ai_group_reply_v1: bool = False
    qq_ai_smart_reply_v1: bool = False
    qq_comfy_drawing_v1: bool = False

    @model_validator(mode="after")
    def headed_contract(self) -> "RuntimeIdentity":
        if self.qq_comfy_drawing_v1 and self.role != "qq":
            raise ValueError("qq_comfy_drawing_manifest_invalid")
        if self.qq_ai_smart_reply_v1 and not self.qq_ai_group_reply_v1:
            raise ValueError("qq_ai_smart_reply_manifest_invalid")
        if self.qq_ai_group_reply_v1 and not self.qq_ai_network_routes_v1:
            raise ValueError("qq_ai_group_reply_manifest_invalid")
        if self.qq_ai_network_routes_v1 and not self.qq_ai_personas_v1:
            raise ValueError("qq_ai_network_routes_manifest_invalid")
        if self.qq_ai_personas_v1 and not self.qq_ai_v1:
            raise ValueError("qq_ai_personas_manifest_invalid")
        if self.qq_ai_v1 and (self.role != "qq" or not self.qq_command_replies_v1):
            raise ValueError("qq_ai_manifest_invalid")
        if self.qq_freqtrade_futures_v1 and not self.qq_freqtrade_v1:
            raise ValueError('qq_freqtrade_futures_manifest_invalid')
        if self.qq_freqtrade_visual_v1 and not self.qq_freqtrade_futures_v1:
            raise ValueError('qq_freqtrade_visual_manifest_invalid')
        if self.qq_command_replies_v1 and not self.qq_freqtrade_capital_v1:
            raise ValueError("qq_command_replies_manifest_invalid")
        if self.qq_freqtrade_capital_v1 and not self.qq_freqtrade_visual_v1:
            raise ValueError('qq_freqtrade_capital_manifest_invalid')
        if self.qq_freqtrade_v1 and not self.qq_quant_v1:
            raise ValueError('qq_freqtrade_manifest_invalid')
        if self.qq_quant_v1 and (self.role != "qq" or self.qq_protocol_mode != "pmhq"):
            raise ValueError("qq_quant_manifest_invalid")
        if self.qq_protocol_mode == "pmhq" and (
            self.role != "qq" or not self.qq_headed_v1 or not self.qq_client_version
            or not self.pmhq_version or self.qq_device_identity_v1
            or self.qq_device_name_override
        ):
            raise ValueError("qq_headed_manifest_invalid")
        return self


def read_runtime_identity(
    path: Path = Path("/opt/al1s/runtime-manifest.json"),
) -> RuntimeIdentity:
    if path.stat().st_size > 16_384:
        raise ValueError("runtime_manifest_too_large")
    return RuntimeIdentity.model_validate(json.loads(path.read_text(encoding="utf-8")))
