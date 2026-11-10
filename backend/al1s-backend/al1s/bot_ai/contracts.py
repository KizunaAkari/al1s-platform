from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from al1s.meme_workshop.contracts import CommandSource
from al1s.bot_ai.managed.smart_contract import AiSmartOptions, SmartObservation

QqId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
LOCAL_GPU_ERROR_CODES = frozenset({"gpu_queue_full", "gpu_user_busy", "comfy_timeout",
                                   "comfy_resource_unavailable", "comfy_cleanup_pending", "gpu_vision_disabled"})

TOOL_ARGUMENTS = {
    'query_trading': {'market', 'kind', 'page'},
    'crypto_chart': {'symbol', 'market', 'period'},
    'market_quote': {'market', 'symbol'},
    'search_feeds': {'query'},
    'send_meme': {'template_key', 'target'},
}


class ProtocolError(ValueError):
    pass




class AiPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text", "image", "record"]
    text: str | None = Field(default=None, max_length=16384)
    media_ref: str | None = Field(default=None, max_length=512)
    fallback_text: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def content_matches_type(self):
        if self.fallback_text is not None and self.type != "record":
            raise ValueError("ai_audio_fallback_invalid")
        if self.type == "text":
            if not self.text or self.media_ref is not None:
                raise ValueError("ai_text_part_invalid")
        elif not self.media_ref or self.text is not None:
            raise ValueError("ai_media_part_invalid")
        return self


class AiInputPart(AiPart):
    original_ref: str | None = Field(default=None, max_length=512)
    caption_hint: str = Field(default="", max_length=1024)
    observation_only: bool = False

    @model_validator(mode="after")
    def image_metadata_only(self):
        if self.fallback_text is not None:
            raise ValueError("ai_input_fallback_invalid")
        if self.type != "image" and (self.original_ref or self.caption_hint or self.observation_only):
            raise ValueError("ai_image_metadata_invalid")
        return self


class AiEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    service_id: UUID
    source: CommandSource
    self_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    sender_name: str = Field(max_length=128)
    mentions: tuple[QqId, ...] = Field(default=(), max_length=4)
    tts_enabled: bool = False
    parts: tuple[AiInputPart, ...] = Field(min_length=1, max_length=6)
    config_version: int = Field(ge=1)
    chat_profile_id: UUID | None = None
    persona_prompt: str = Field(default="", max_length=12000)
    smart_reply: AiSmartOptions | None = None
    sticker_enabled: bool = False
    addressed: bool = False
    smart_context: tuple[SmartObservation, ...] = Field(default=(), max_length=12)
    vision_provider_id: str | None = Field(default=None, max_length=128)
    deadline: datetime

    @field_validator("source")
    @classmethod
    def qq_source(cls, source: CommandSource) -> CommandSource:
        if source.scope_kind not in {"qq_group", "qq_private"}:
            raise ValueError("ai_qq_source_required")
        return source

    @field_validator("deadline")
    @classmethod
    def deadline_is_aware(cls, deadline: datetime) -> datetime:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("ai_deadline_requires_timezone")
        return deadline.astimezone(UTC)

