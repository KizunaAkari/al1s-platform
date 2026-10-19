from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

QqProtocolMode = Literal["direct", "pmhq"]


class HeadedVersions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    qq_client_version: str = Field(min_length=1, max_length=64)
    pmhq_version: str = Field(min_length=1, max_length=64)


class HeadedHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ready: bool
    online: bool
    account: str | None = Field(default=None, pattern=r"^[1-9][0-9]{4,19}$")


class ClientProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instance_id: UUID
    account: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    schema_version: Literal[1] = 1
