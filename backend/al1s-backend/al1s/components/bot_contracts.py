from datetime import datetime
from typing import Literal

from pydantic import Field

from al1s.components.contracts import ComponentInventoryEntry, Contract

BotAlias = Literal["qq", "discord"]


class QqCandidateDraft(Contract):
    native_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    architecture: Literal["amd64", "arm64"]
    registry_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    platform_manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    runtime_artifact_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    runtime_version: str = Field(pattern=r"^[0-9][A-Za-z0-9._+-]{0,31}$")
    source_url: str | None = Field(default=None, max_length=256)
    source_published_at: datetime | None = None
    source_notes: str | None = Field(default=None, max_length=4000)


class BuiltQqCandidate(Contract):
    candidate_image: str
    expected_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    role: Literal["qq"] = "qq"
    native_version: str
    runtime_version: str
    proof: bool


class BotUpdateState(Contract):
    alias: BotAlias
    current: ComponentInventoryEntry
    runtime_version: str | None = None
