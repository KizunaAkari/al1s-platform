import json
from contextlib import suppress
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from al1s.bots.qq_headed.contracts import QqProtocolMode
from al1s.components.bot_contracts import BotAlias
from al1s.components.contracts import Contract
from al1s.components.host.commands import CommandRunner


class RuntimeProof(Contract):
    role: BotAlias
    native_version: str = Field(min_length=1, max_length=64)
    runtime_version: str = Field(min_length=1, max_length=64)
    render_proof: Literal[True]
    qq_device_identity_v1: bool = False
    qq_device_name_override: bool = False
    qq_protocol_mode: QqProtocolMode = "direct"
    qq_client_version: str | None = Field(default=None, min_length=1, max_length=64)
    pmhq_version: str | None = Field(default=None, min_length=1, max_length=64)
    qq_headed_v1: bool = False

    @model_validator(mode="after")
    def headed_contract(self) -> "RuntimeProof":
        if self.qq_protocol_mode == "pmhq" and (
            self.role != "qq" or not self.qq_headed_v1 or not self.qq_client_version
            or not self.pmhq_version or self.qq_device_identity_v1
            or self.qq_device_name_override
        ):
            raise ValueError("qq_headed_proof_invalid")
        return self


def probe_bot_image(runner: CommandRunner, image_id: str) -> RuntimeProof:
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError("bot_image_identity_invalid")
    name = "al1s-component-probe-" + uuid4().hex[:12]
    try:
        output = runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--name",
                name,
                "--label",
                "al1s.component-probe=true",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--entrypoint",
                "python",
                image_id,
                "-m",
                "al1s.meme_workshop.runtime_probe",
            ],
            60,
        )
        return RuntimeProof.model_validate(json.loads(output))
    finally:
        with suppress(Exception):
            runner.run(["docker", "rm", "-f", name], 15)
