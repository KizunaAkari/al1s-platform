from datetime import date, time
from typing import Any, Literal
from pydantic import BaseModel, Field


class AgentRegistration(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str
    os: Literal["windows", "linux", "macos", "unknown"] = "unknown"
    address: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Heartbeat(BaseModel):
    device: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCreate(BaseModel):
    agent_id: str
    name: str = "MAA task"
    script: str | None = None
    script_name: str | None = Field(default=None, max_length=240)
    params: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=0, ge=0, le=20)
    record_video: bool = False


class TaskBatchItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    script: str = Field(min_length=1)
    script_name: str | None = Field(default=None, max_length=240)
    params: dict[str, Any] = Field(default_factory=dict)


class TaskBatchCreate(BaseModel):
    agent_id: str
    items: list[TaskBatchItem] = Field(min_length=1, max_length=50)
    mode: Literal["once", "repeat", "scheduled"] = "once"
    repeat_count: int = Field(default=1, ge=1, le=100)
    schedule_start_date: date | None = None
    schedule_end_date: date | None = None
    daily_start_time: time | None = None
    daily_end_time: time | None = None
    max_retries: int = Field(default=0, ge=0, le=20)
    record_video: bool = False


class TaskCompositionModule(BaseModel):
    script_name: str = Field(min_length=1, max_length=200)
    interval_after_seconds: float = Field(default=0, ge=0, le=3600)


class TaskCompositionCreate(BaseModel):
    agent_id: str
    name: str = Field(default="组合任务", min_length=1, max_length=200)
    modules: list[TaskCompositionModule] = Field(min_length=2, max_length=50)
    params: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["once", "repeat", "scheduled"] = "once"
    repeat_count: int = Field(default=1, ge=1, le=100)
    schedule_start_date: date | None = None
    schedule_end_date: date | None = None
    daily_start_time: time | None = None
    daily_end_time: time | None = None
    max_retries: int = Field(default=0, ge=0, le=20)
    record_video: bool = False


class MaintenanceCleanup(BaseModel):
    target: Literal["recordings", "task_history", "all"]
    older_than_days: int = Field(default=30, ge=0, le=3650)
    include_failure_evidence: bool = False


class CommandResult(BaseModel):
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)


class LogCreate(BaseModel):
    level: str = "INFO"
    message: str


class ScriptPayload(BaseModel):
    content: str


class ScriptImportPayload(ScriptPayload):
    name: str = Field(min_length=1, max_length=240)
    category_package: str | None = Field(default=None, max_length=255)


class ScriptCategoryUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class ScriptCategoryMove(BaseModel):
    category_package: str | None = Field(default=None, max_length=255)


class ScriptStepPayload(ScriptPayload):
    keep_interactive: bool = True


class QuickTestPayload(ScriptPayload):
    mode: Literal["once", "repeat"] = "once"
    repeat_count: int = Field(default=1, ge=1, le=100)


class DeviceControl(BaseModel):
    action: Literal["tap", "swipe", "back", "home", "wake", "sleep", "orientation"]
    x: int | None = Field(default=None, ge=0, le=20_000)
    y: int | None = Field(default=None, ge=0, le=20_000)
    x1: int | None = Field(default=None, ge=0, le=20_000)
    y1: int | None = Field(default=None, ge=0, le=20_000)
    x2: int | None = Field(default=None, ge=0, le=20_000)
    y2: int | None = Field(default=None, ge=0, le=20_000)
    duration_ms: int = Field(default=300, ge=50, le=5_000)
    capture_after: bool = True
    orientation: Literal["auto", "portrait", "landscape"] | None = None


class InteractiveStart(BaseModel):
    device_serial: str = Field(min_length=1, max_length=200)


class InteractiveStop(BaseModel):
    session_token: str = Field(min_length=16, max_length=200)


class TerminalDeployRequest(BaseModel):
    artifact_name: str = Field(min_length=1, max_length=240)


class EmailMessage(BaseModel):
    to: list[str] = Field(min_length=1)
    subject: str
    body: str
    html: bool = False


class NotificationSettingsUpdate(BaseModel):
    smtp_host: str = Field(default="", max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = Field(default="", max_length=255)
    smtp_password: str | None = Field(default=None, max_length=1000)
    clear_password: bool = False
    smtp_from: str = Field(default="", max_length=320)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    failure_recipients: list[str] = Field(default_factory=list, max_length=20)
    failure_enabled: bool = False
    public_base_url: str = Field(default="http://127.0.0.1:8000", max_length=500)


class TestEmailRequest(BaseModel):
    to: str | None = Field(default=None, max_length=320)
