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
    qq_quant_v1: bool = False
    qq_freqtrade_v1: bool = False
    qq_freqtrade_futures_v1: bool = False
    qq_freqtrade_visual_v1: bool = False
    qq_freqtrade_capital_v1: bool = False
    qq_command_replies_v1: bool = False
    qq_ai_v1: bool = False
    qq_ai_personas_v1: bool = False
    qq_ai_network_routes_v1: bool = False
    qq_ai_group_reply_v1: bool = False
    qq_ai_smart_reply_v1: bool = False
    qq_comfy_drawing_v1: bool = False

    @model_validator(mode="after")
    def headed_contract(self) -> "RuntimeProof":
        if self.qq_comfy_drawing_v1 and self.role != "qq":
            raise ValueError("qq_comfy_drawing_proof_invalid")
        if self.qq_ai_smart_reply_v1 and not self.qq_ai_group_reply_v1:
            raise ValueError("qq_ai_smart_reply_proof_invalid")
        if self.qq_ai_group_reply_v1 and not self.qq_ai_network_routes_v1:
            raise ValueError("qq_ai_group_reply_proof_invalid")
        if self.qq_ai_network_routes_v1 and not self.qq_ai_personas_v1:
            raise ValueError("qq_ai_network_routes_proof_invalid")
        if self.qq_ai_personas_v1 and not self.qq_ai_v1:
            raise ValueError("qq_ai_personas_proof_invalid")
        if self.qq_ai_v1 and (self.role != "qq" or not self.qq_command_replies_v1):
            raise ValueError("qq_ai_proof_invalid")
        if self.qq_freqtrade_futures_v1 and not self.qq_freqtrade_v1:
            raise ValueError('qq_freqtrade_futures_proof_invalid')
        if self.qq_freqtrade_visual_v1 and not self.qq_freqtrade_futures_v1:
            raise ValueError('qq_freqtrade_visual_proof_invalid')
        if self.qq_command_replies_v1 and not self.qq_freqtrade_capital_v1:
            raise ValueError("qq_command_replies_proof_invalid")
        if self.qq_freqtrade_capital_v1 and not self.qq_freqtrade_visual_v1:
            raise ValueError('qq_freqtrade_capital_proof_invalid')
        if self.qq_freqtrade_v1 and not self.qq_quant_v1:
            raise ValueError('qq_freqtrade_proof_invalid')
        if self.qq_quant_v1 and (self.role != "qq" or self.qq_protocol_mode != "pmhq"):
            raise ValueError("qq_quant_proof_invalid")
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
