from dataclasses import dataclass, field
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Slot(StrictModel):
    x: float = Field(default=0, ge=0, le=2048)
    y: float = Field(default=0, ge=0, le=2048)
    width: float = Field(default=128, ge=1, le=2048)
    height: float = Field(default=128, ge=1, le=2048)
    order: int = Field(default=0, ge=0, le=7)


class AvatarSlot(Slot):
    shape: Literal["circle", "rectangle"] = "circle"
    fit: Literal["cover", "contain"] = "cover"
    rotation: float = Field(default=0, ge=-180, le=180)


class TextSlot(Slot):
    font_size: int = Field(default=28, ge=8, le=256)
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    stroke_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    stroke_width: int = Field(default=1, ge=0, le=8)
    align: Literal["left", "center", "right"] = "center"


class Layout(StrictModel):
    schema_version: Literal[1] = 1
    engine: Literal["composite", "petpet", "caption"] = "composite"
    width: int = Field(default=320, ge=32, le=2048)
    height: int = Field(default=320, ge=32, le=2048)
    avatars: list[AvatarSlot] = Field(default_factory=list, max_length=4)
    texts: list[TextSlot] = Field(default_factory=list, max_length=4)
    frame_duration_ms: int = Field(default=100, ge=40, le=500)
    background_color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def bounded_slots(self) -> Self:
        if any(
            slot.x + slot.width > self.width or slot.y + slot.height > self.height
            for slot in [*self.avatars, *self.texts]
        ):
            raise ValueError("槽位必须位于画布内")
        return self

    def requirements(self) -> tuple[int, int, int]:
        if self.engine == "petpet":
            return (1, 0, 0)
        if self.engine == "caption":
            return (0, 1, 1)
        return (len(self.avatars), 0, len(self.texts))


class TemplateWrite(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    keyword: str = Field(min_length=1, max_length=32)
    aliases: list[str] = Field(default_factory=list, max_length=8)
    enabled: bool = True
    random_enabled: bool = True
    layout: Layout = Field(default_factory=Layout)
    background_blob_id: UUID | None = None
    foreground_blob_id: UUID | None = None

    @model_validator(mode="after")
    def valid_words(self) -> Self:
        import unicodedata

        self.name = self.name.strip()
        words = [
            unicodedata.normalize("NFKC", word).strip().casefold()
            for word in [self.keyword, *self.aliases]
        ]
        if (
            not self.name
            or any(not word or len(word) > 32 or any(c.isspace() for c in word) for word in words)
            or len(set(words)) != len(words)
        ):
            raise ValueError("关键词必须非空、不含空格且不能重复")
        self.keyword, self.aliases = words[0], words[1:]
        return self


class Scope(StrictModel):
    kind: Literal["qq_group", "discord_channel"]
    target_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    guild_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{16,19}$")


class ConnectionSettings(StrictModel):
    enabled: bool = False
    private_enabled: bool = False
    qq_event_ws_url: str | None = Field(default=None, max_length=1024)
    cooldown_seconds: int = Field(default=5, ge=1, le=60)
    scopes: list[Scope] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_scopes(self) -> Self:
        from urllib.parse import urlsplit

        keys = [(scope.kind, scope.target_id) for scope in self.scopes]
        if len(keys) != len(set(keys)):
            raise ValueError("群或频道不能重复")
        if self.qq_event_ws_url:
            parsed = urlsplit(self.qq_event_ws_url)
            if (
                parsed.scheme not in {"ws", "wss"}
                or not parsed.hostname
                or any((parsed.username, parsed.password, parsed.query, parsed.fragment))
            ):
                raise ValueError("需要不含凭据的OneBot WebSocket地址")
        return self


class CommandSource(StrictModel):
    message_id: str = Field(min_length=1, max_length=64)
    scope_kind: Literal["qq_group", "qq_private", "discord_channel", "discord_private"]
    target_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    sender_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    guild_id: str | None = None


@dataclass(frozen=True)
class RenderInputs:
    avatars: list[bytes] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderedImage:
    body: bytes
    media_type: str
    width: int
    height: int
