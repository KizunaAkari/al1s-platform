from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_PACKAGE_BYTES = 5 * 1024**3
MEDIA_TYPE = "application/x-tar"


class ReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    release_id: UUID
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    architecture: Literal["arm64"] = "arm64"
    size_bytes: int = Field(gt=0, le=MAX_PACKAGE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_image: str = Field(pattern=r"^al1s-terminal-next:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    expected_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class Release(ReleaseInput):
    state: Literal["draft", "queued", "verifying", "published", "failed"]
    created_at: datetime
    published_at: datetime | None = None
    error_code: str | None = None
    blob_id: UUID | None = None
    row_version: int


class VerificationJob(BaseModel):
    release: Release
    candidate_blob_id: UUID
    destination_key: str


def staging_key(identity: UUID) -> str:
    return f"linux-release-uploads/{identity}"
