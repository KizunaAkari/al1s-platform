from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

UpgradeUnit = Literal["platform", "postgresql", "s3", "mqtt", "linux-terminal"]
ComponentKey = Literal["platform", "postgresql", "s3", "mqtt", "linux-terminal", "maa"]
MAX_PACKAGE_BYTES = 5 * 1024**3
REPOSITORIES: dict[str, tuple[str, ...]] = {
    "platform": ("al1s-platform",),
    "linux-terminal": ("al1s-terminal", "al1s-terminal-next"),
    "postgresql": ("postgres", "m.daocloud.io/docker.io/library/postgres"),
    "s3": ("chrislusf/seaweedfs", "m.daocloud.io/docker.io/chrislusf/seaweedfs"),
    "mqtt": ("eclipse-mosquitto", "m.daocloud.io/docker.io/library/eclipse-mosquitto"),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ComponentTarget(Contract):
    kind: Literal["platform", "terminal"]
    terminal_id: UUID | None = None

    @model_validator(mode="after")
    def bound_target(self) -> Self:
        if (self.kind == "terminal") != (self.terminal_id is not None):
            raise ValueError("terminal target requires a UUID; platform target has no terminal ID")
        return self

    @property
    def scope(self) -> str:
        return "platform" if self.kind == "platform" else f"terminal:{self.terminal_id}"


class ComponentInventoryEntry(Contract):
    component: ComponentKey
    upgrade_unit: UpgradeUnit
    target: ComponentTarget
    display_name: str
    version: str | None = Field(default=None, max_length=128)
    architecture: str | None = None
    status: Literal["healthy", "offline", "unhealthy", "unknown"]
    observed_at: datetime | None = None
    image: str | None = None
    image_id: str | None = None
    source_identity: str | None = None
    reason: str | None = None


class InventoryPage(Contract):
    items: list[ComponentInventoryEntry]
    next_cursor: UUID | None = None


class ComponentReleaseInput(Contract):
    release_id: UUID
    component: UpgradeUnit
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
    architecture: Literal["amd64", "arm64"]
    size_bytes: int = Field(gt=0, le=MAX_PACKAGE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_image: str = Field(max_length=256)
    expected_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    native_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
    compatible_from: str = Field(min_length=1, max_length=128)
    data_policy: Literal["same-format", "alembic"] = "same-format"
    schema_from: str | None = Field(default=None, max_length=64)
    schema_to: str | None = Field(default=None, max_length=64)
    maa_version: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def bound_package(self) -> Self:
        import re

        repository, separator, tag = self.candidate_image.rpartition(":")
        if self.component == "linux-terminal" and self.architecture != "arm64":
            raise ValueError("the existing Linux upgrade driver supports ARM64 releases")
        if (
            not separator
            or repository not in REPOSITORIES[self.component]
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag)
            or tag in {"latest", "stable", "rollback"}
        ):
            raise ValueError("use an immutable image tag in the registered component repository")
        if self.data_policy == "alembic" and (
            self.component != "platform" or not self.schema_from or not self.schema_to
        ):
            raise ValueError("platform migration requires a complete source and target revision")
        if self.maa_version is not None and self.component != "linux-terminal":
            raise ValueError("MaaFramework is part of a Linux terminal release")
        return self


class ComponentRelease(ComponentReleaseInput):
    state: Literal["draft", "queued", "verifying", "published", "failed"]
    row_version: int
    blob_id: UUID | None = None
    created_at: datetime
    published_at: datetime | None = None
    error_code: str | None = None


class ReleasePage(Contract):
    items: list[ComponentRelease]
    next_cursor: UUID | None = None


OperationState = Literal[
    "accepted", "preparing", "switching", "verifying", "succeeded", "failed_safe", "unknown"
]


class ComponentPreflight(Contract):
    component: UpgradeUnit
    target: ComponentTarget
    release_id: UUID
    expected_source: str
    current_version: str | None
    target_version: str
    allowed: bool
    reasons: list[str]
    affected_services: list[str]
    backup_required: bool


class ComponentUpgradeRequest(Contract):
    component: UpgradeUnit
    target: ComponentTarget
    release_id: UUID
    expected_source: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class ComponentOperation(Contract):
    operation_id: UUID
    component: UpgradeUnit
    target: ComponentTarget
    release_id: UUID
    expected_source: str
    state: OperationState
    stage: str
    created_at: datetime
    updated_at: datetime
    row_version: int
    error_code: str | None = None
    recovery_point: str | None = None
    status_token: str | None = None


class OperationPage(Contract):
    items: list[ComponentOperation]
    next_cursor: UUID | None = None
