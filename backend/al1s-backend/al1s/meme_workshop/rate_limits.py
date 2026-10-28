from typing import Literal

from pydantic import Field

from al1s.meme_workshop.contracts import StrictModel

MemePlatform = Literal["discord", "qq"]


class RateLimitWrite(StrictModel):
    interval_seconds: int = Field(strict=True, ge=1, le=3600)


class RateLimitRecord(RateLimitWrite):
    platform: MemePlatform
    row_version: int
