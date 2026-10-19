from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.components.host.bot_deployment import BotDeploymentSettings
from al1s.execution.host_management import HostManagerConnection


class HostSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project: str = Field(default="al1s-platform", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    container_name_prefix: str = Field(
        default="al1s-platform-", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"
    )
    deployment_root: Path
    compose_files: list[Path] = Field(min_length=1, max_length=16)
    env_files: list[Path] = Field(min_length=1, max_length=16)
    selectors_file: Path
    state_dir: Path
    platform: HostManagerConnection
    certificate_file: Path
    private_key_file: Path
    listen: str = "0.0.0.0"
    port: int = Field(default=8012, ge=1024, le=65535)
    allowed_origin: str
    bots: BotDeploymentSettings | None = None
    qq_runtime_artifact_digest: str = ""
    qq_runtime_version: str = ""

    @model_validator(mode="after")
    def registered_paths(self) -> Self:
        root = self.deployment_root.resolve()
        for path in [*self.compose_files, *self.env_files, self.selectors_file]:
            if not path.resolve().is_relative_to(root):
                raise ValueError(
                    "compose and version selectors must stay inside registered deployment root"
                )
        return self

    def compose_arguments(self) -> list[str]:
        values = ["docker", "compose", "-p", self.project]
        for path in self.env_files:
            values.extend(["--env-file", str(path)])
        for path in self.compose_files:
            values.extend(["-f", str(path)])
        return values
