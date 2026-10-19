from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.bots.qq_identity.contracts import QqIdentitySummary

UpgradeUnit = Literal[
    "platform", "postgresql", "s3", "mqtt", "linux-terminal", "qq-bot", "discord-bot"
]
ComponentKey = Literal[
    "platform", "postgresql", "s3", "mqtt", "linux-terminal", "maa", "qq-bot", "discord-bot"
]
MAX_PACKAGE_BYTES = 5 * 1024**3
REPOSITORIES: dict[str, tuple[str, ...]] = {
    "platform": ("al1s-platform",),
    "linux-terminal": ("al1s-terminal", "al1s-terminal-next"),
    "postgresql": ("postgres", "m.daocloud.io/docker.io/library/postgres"),
    "s3": (
        "chrislusf/seaweedfs",
        "m.daocloud.io/docker.io/chrislusf/seaweedfs",
        "al1s-platform-storage",
    ),
    "mqtt": ("eclipse-mosquitto", "m.daocloud.io/docker.io/library/eclipse-mosquitto"),
    "qq-bot": ("al1s-plachta",),
    "discord-bot": ("al1s-plachta",),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ComponentTarget(Contract):
    kind: Literal["platform", "terminal", "bot"]
    terminal_id: UUID | None = None
    bot_alias: Literal["qq", "discord"] | None = None

    @model_validator(mode="after")
    def bound_target(self) -> Self:
        if (self.kind == "terminal") != (self.terminal_id is not None):
            raise ValueError("terminal target requires a UUID; platform target has no terminal ID")
        if (self.kind == "bot") != (self.bot_alias is not None):
            raise ValueError("bot target requires its fixed alias")
        return self

    @property
    def scope(self) -> str:
        if self.kind == "bot":
            return f"bot:{self.bot_alias}"
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
    storage_layout: Literal["legacy", "bundle"] | None = None
    reason: str | None = None
    runtime_version: str | None = None
    qq_identity: QqIdentitySummary | None = None
    qq_protocol_mode: Literal["direct", "pmhq"] = "direct"
    qq_client_version: str | None = Field(default=None, max_length=64)
    pmhq_version: str | None = Field(default=None, max_length=64)


class InventoryPage(Contract):
    items: list[ComponentInventoryEntry]
    next_cursor: UUID | None = None


class ComponentReleaseInput(Contract):
    release_id: UUID
    component: UpgradeUnit
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
    architecture: Literal["amd64", "arm64"]
    size_bytes: int | None = Field(default=None, gt=0, le=MAX_PACKAGE_BYTES)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    candidate_image: str | None = Field(default=None, max_length=256)
    expected_image_id: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    native_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
    compatible_from: str = Field(min_length=1, max_length=128)
    data_policy: Literal["same-format", "alembic"] = "same-format"
    schema_from: str | None = Field(default=None, max_length=64)
    schema_to: str | None = Field(default=None, max_length=64)
    maa_version: str | None = Field(default=None, max_length=64)
    source_kind: Literal["archive", "registry"] = "archive"
    registry_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    platform_manifest_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    runtime_artifact_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    runtime_version: str | None = Field(default=None, max_length=64)
    source_url: str | None = Field(
        default=None,
        max_length=256,
        pattern=r"^https://github\.com/LLOneBot/LuckyLilliaBot/releases/tag/v8\.[0-9]{1,4}\.[0-9]{1,4}$",
    )
    source_published_at: datetime | None = None
    source_notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def bound_package(self) -> Self:
        import re

        if self.source_kind == "registry":
            if self.component != "qq-bot" or not all(
                (
                    self.registry_digest,
                    self.platform_manifest_digest,
                    self.runtime_artifact_digest,
                    self.runtime_version,
                )
            ):
                raise ValueError("registry candidates require the fixed QQ source and runtime")
            if (
                self.size_bytes is not None
                or self.sha256 is not None
                or self.data_policy != "same-format"
                or self.schema_from is not None
                or self.schema_to is not None
                or self.maa_version is not None
                or getattr(self, "blob_id", None) is not None
            ):
                raise ValueError("registry source has no archive or database migration")
            if (self.candidate_image is None) != (self.expected_image_id is None):
                raise ValueError("registry image identity must be complete")
            if getattr(self, "state", None) == "published" and not (
                self.candidate_image and self.expected_image_id
            ):
                raise ValueError("published QQ candidate requires its complete image")
            if self.candidate_image is None:
                return self
        elif not all((self.size_bytes, self.sha256, self.candidate_image, self.expected_image_id)):
            raise ValueError("archive source requires complete package identity")
        assert self.candidate_image is not None
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

    @model_validator(mode="after")
    def match_bot(self) -> Self:
        if self.component in {"qq-bot", "discord-bot"}:
            if self.target.kind != "bot" or self.component != f"{self.target.bot_alias}-bot":
                raise ValueError("bot component and target mismatch")
        elif self.target.kind == "bot":
            raise ValueError("bot target is only for bot components")
        return self


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
