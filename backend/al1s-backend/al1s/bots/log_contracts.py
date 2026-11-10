from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LogSource = Literal["all", "bot", "backend", "gateway", "ops"]
LogLevel = Literal["all", "important"]


class LogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occurred_at: datetime
    source: Literal["bot", "backend", "gateway", "ops"]
    level: Literal["info", "warning", "error"]
    message: str = Field(max_length=501)


class LogPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: datetime
    items: list[LogEntry] = Field(max_length=100)
    scan_limited: bool
