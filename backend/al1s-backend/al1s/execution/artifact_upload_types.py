from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ArtifactOwnerKind(StrEnum):
    FORMAL_ATTEMPT = "formal_attempt"
    QUICK_TEST = "quick_test"


class ArtifactKind(StrEnum):
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    LOG = "log"
    DIAGNOSTIC = "diagnostic"


class ArtifactStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ArtifactUploadRecord:
    artifact_id: UUID
    terminal_id: UUID
    owner_kind: ArtifactOwnerKind
    owner_id: UUID
    artifact_kind: ArtifactKind
    file_name: str
    expected_sha256: str
    expected_size_bytes: int
    media_type: str
    object_key: str
    status: ArtifactStatus
    idempotency_key: str
    expires_at: datetime
    completed_at: datetime | None
    blob_id: UUID | None
    created_at: datetime
    row_version: int


@dataclass(frozen=True, slots=True)
class PresignedArtifactUpload:
    method: str
    url: str
    headers: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactObjectHead:
    size_bytes: int
    media_type: str
    sha256: str | None
