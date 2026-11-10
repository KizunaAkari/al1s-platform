from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
import unicodedata
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from al1s.bot_ai.managed.smart_contract import AiSmartOptions, local_endpoint

QqId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
Role = Literal["chat", "vision", "stt", "tts"]
NetworkMode = Literal["direct", "proxy"]
ADAPTERS = {
    "chat": {"openai_chat_completion", "openai_responses", "googlegenai_chat_completion"},
    "vision": {"follow_chat", "openai_chat_completion", "openai_responses",
               "googlegenai_chat_completion", "al1s_comfyui_vision"},
    "stt": {"openai_whisper_api"},
    "tts": {"gemini_tts", "openai_tts_api"},
}
ALL_ADAPTERS = set().union(*ADAPTERS.values())


class AiProviderWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_kind: str = Field(max_length=64)
    model_id: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=1024)
    voice: str = Field(default="", max_length=200)
    secret_input: SecretStr | None = Field(default=None, repr=False)
    clear_secret: bool = False
    network_mode: NetworkMode | None = None

    @model_validator(mode="after")
    def validate_provider(self) -> Self:
        if self.adapter_kind not in ALL_ADAPTERS:
            raise ValueError("ai_provider_adapter_invalid")
        if self.adapter_kind != "follow_chat" and not self.model_id.strip():
            raise ValueError("ai_provider_model_required")
        if self.adapter_kind == "al1s_comfyui_vision":
            local_endpoint(self.base_url)
            if self.model_id != "Qwen/Qwen3-VL-4B-Instruct":
                raise ValueError("ai_comfyui_model_unsupported")
            if self.secret_input is not None or self.network_mode == "proxy":
                raise ValueError("ai_comfyui_local_configuration_required")
        if self.secret_input is not None:
            value = self.secret_input.get_secret_value()
            if not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
                raise ValueError("ai_provider_secret_invalid")
            if self.clear_secret:
                raise ValueError("ai_provider_secret_conflict")
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("ai_provider_url_invalid")
        return self


class AiProviderView(BaseModel):
    adapter_kind: str
    model_id: str
    base_url: str
    voice: str
    secret_configured: bool
    network_mode: NetworkMode = "direct"

    @property
    def configured(self) -> bool:
        return bool(self.model_id.strip() and (
            self.secret_configured or self.adapter_kind == "al1s_comfyui_vision"))


class AiChatProfileView(BaseModel):
    id: UUID
    name: str
    description: str
    persona: str
    adapter_kind: str
    model_id: str
    base_url: str
    secret_configured: bool
    supports_vision: bool
    is_default: bool
    network_mode: NetworkMode = "direct"


class AiChatProfileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=32)
    description: str = Field(default="", max_length=500)
    persona: str = Field(default="", max_length=12000)
    adapter_kind: str = Field(max_length=64)
    model_id: str = Field(max_length=200)
    base_url: str = Field(default="", max_length=1024)
    secret_input: SecretStr | None = Field(default=None, repr=False)
    clear_secret: bool = False
    supports_vision: bool = False
    is_default: bool = False
    network_mode: NetworkMode | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        if not value or len(value) > 32 or any(not c.isprintable() for c in value):
            raise ValueError("ai_profile_name_invalid")
        return value

    @model_validator(mode="after")
    def valid_chat_provider(self) -> Self:
        if self.adapter_kind not in ADAPTERS["chat"]:
            raise ValueError("ai_profile_adapter_invalid")
        AiProviderWrite(adapter_kind=self.adapter_kind, model_id=self.model_id,
                        base_url=self.base_url, secret_input=self.secret_input,
                        clear_secret=self.clear_secret)
        return self


class AiSettingsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    event_ws_url: str | None = Field(default=None, max_length=1024)
    allow_private: bool = False
    groups: list[QqId] = Field(default_factory=list, max_length=100)
    wake_words: list[str] = Field(default_factory=list, max_length=10)
    persona: str = Field(default="", max_length=12000)
    vision_enabled: bool = False
    stt_enabled: bool = False
    tts_mode: Literal["off", "voice_input", "always"] = "off"

    @field_validator("groups")
    @classmethod
    def unique_groups(cls, groups: list[str]) -> list[str]:
        if len(set(groups)) != len(groups):
            raise ValueError("ai_groups_duplicate")
        return groups

    @field_validator("wake_words")
    @classmethod
    def valid_wake_words(cls, words: list[str]) -> list[str]:
        normalized = [unicodedata.normalize("NFKC", word).strip() for word in words]
        if any(not word or len(word) > 32 for word in normalized):
            raise ValueError("ai_wake_word_invalid")
        if len({word.casefold() for word in normalized}) != len(normalized):
            raise ValueError("ai_wake_word_duplicate")
        return normalized

    @field_validator("event_ws_url")
    @classmethod
    def valid_event_url(cls, value: str | None) -> str | None:
        if value:
            parsed = urlsplit(value)
            if (parsed.scheme not in {"ws", "wss"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("ai_event_url_invalid")
        return value or None


class AiSettingsWrite(AiSettingsBase):
    providers: dict[Role, AiProviderWrite] = Field(default_factory=dict)
    reply_all_groups: list[QqId] | None = Field(default=None, max_length=100)
    smart_reply_groups: list[QqId] | None = Field(default=None, max_length=100)
    smart_reply: AiSmartOptions | None = None

    @model_validator(mode="after")
    def check_enabled(self) -> Self:
        if self.reply_all_groups is not None and (
                len(set(self.reply_all_groups)) != len(self.reply_all_groups)
                or not set(self.reply_all_groups).issubset(self.groups)):
            raise ValueError("ai_reply_all_groups_invalid")
        if self.smart_reply_groups is not None and (
                len(set(self.smart_reply_groups)) != len(self.smart_reply_groups)
                or not set(self.smart_reply_groups).issubset(self.groups)
                or set(self.smart_reply_groups).intersection(self.reply_all_groups or [])):
            raise ValueError("ai_smart_reply_groups_invalid")
        if self.enabled and (not self.event_ws_url or "chat" not in self.providers):
            raise ValueError("ai_connection_and_chat_required")
        if self.enabled and ((self.stt_enabled and "stt" not in self.providers)
                             or (self.tts_mode != "off" and "tts" not in self.providers)):
            raise ValueError("ai_enabled_provider_required")
        for role, provider in self.providers.items():
            if provider.adapter_kind not in ADAPTERS[role]:
                raise ValueError("ai_provider_role_invalid")
        return self


class AiSettingsRecord(AiSettingsBase):
    service_id: UUID
    reply_all_groups: list[QqId] = Field(default_factory=list, max_length=100)
    smart_reply_groups: list[QqId] = Field(default_factory=list, max_length=100)
    smart_reply: AiSmartOptions = Field(default_factory=AiSmartOptions)
    providers: dict[Role, AiProviderView] = Field(default_factory=dict)
    desired_version: int = 0
    applied_version: int = 0
    last_error: str | None = None
