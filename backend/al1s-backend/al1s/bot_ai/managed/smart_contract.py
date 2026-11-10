from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SMART_RESULT_CODES = frozenset({
    "ai_silent", "ai_decision_failed", "ai_rate_limited", "ai_coarse_skipped",
    "ai_media_unavailable",
    "ai_decision_provider_missing", "ai_decision_timeout", "ai_decision_provider_error",
    "ai_decision_json_invalid", "ai_decision_schema_invalid",
})


def display_name(value, sender_id: str = "", fallback: str = "群友") -> str:
    if not isinstance(value, str):
        return fallback
    name = "".join(char for char in value if char.isprintable()).strip()[:64]
    return name if name and name != sender_id else fallback


def local_endpoint(value):
    value = value or "http://host.docker.internal:8188"
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"host.docker.internal", "localhost", "127.0.0.1", "::1"}
            or parsed.port != 8188 or parsed.path not in {"", "/"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("ai_comfyui_local_address_required")
    return value.rstrip("/")


class AiSmartOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style: Literal["context", "random"] = "context"
    min_interval_seconds: int = Field(default=20, ge=0, le=3600)
    cooldown_seconds: int = Field(default=30, ge=0, le=3600)
    window_seconds: int = Field(default=120, ge=1, le=3600)
    max_replies: int = Field(default=2, ge=1, le=50)
    voice_enabled: bool = False
    stickers_enabled: bool = True
    collect_stickers: bool = True


class SmartObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_id: str = Field(pattern=r"^-?[0-9]{1,64}$")
    sender_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    sender_name: str = Field(default="", max_length=64)
    text: str = Field(max_length=400)
    age_seconds: float = Field(default=0, ge=0, le=120)


class StickerCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "status", "detail", "preview", "enable", "disable", "delete", "edit"]
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    image_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=24, ge=1, le=24)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    overlay_text: str | None = Field(default=None, max_length=80)
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]] | None = Field(default=None, max_length=12)
    scenes: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def requires_image(self):
        if self.action not in {"list", "status"} and not self.image_id:
            raise ValueError("ai_sticker_id_required")
        values = (self.category, self.description, self.overlay_text, self.tags, self.scenes)
        if self.action == "edit" and any(value is None for value in values):
            raise ValueError("ai_sticker_edit_invalid")
        if self.action != "edit" and any(value is not None for value in values):
            raise ValueError("ai_sticker_edit_invalid")
        return self


class StickerImportCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["import"] = "import"
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    request_id: UUID
    media_ref: str = Field(min_length=1, max_length=1024)
    vision_provider_id: str = Field(min_length=1, max_length=128)
