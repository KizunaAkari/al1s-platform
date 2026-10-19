"""Image capability check without credentials, database access, or chat traffic."""

import json
import os
from io import BytesIO
from pathlib import Path

from opencc import OpenCC
from PIL import Image

from al1s.meme_workshop.contracts import Layout, RenderInputs
from al1s.meme_workshop.renderer import render
from al1s.meme_workshop.runtime_identity import read_runtime_identity


def prove_runtime(manifest: Path, package: Path, role: str, *,
                  supervisor: Path = Path("/etc/al1s-qq-supervisord.conf")) -> dict[str, object]:
    identity = read_runtime_identity(manifest)
    if identity.role != role:
        raise ValueError("bot_runtime_role_invalid")
    native = json.loads(package.read_text(encoding="utf-8"))["version"]
    if native != identity.native_version:
        raise ValueError("bot_native_version_mismatch")
    if identity.qq_protocol_mode == "pmhq":
        from al1s.bots.qq_headed.probe import prove_headed
        if os.environ.get("PROTOCOL_MODE", "pmhq") != "pmhq":
            raise ValueError("qq_headed_mode_mismatch")
        prove_headed(identity, supervisor)
    if identity.qq_device_identity_v1:
        from al1s.bots.qq_identity.apply import default_device_name
        if (role != "qq" or os.environ.get("AL1S_QQ_BOOTSTRAP_VERSION") != "1"
                or "command=python -m al1s.bots.qq_identity.native_start"
                not in supervisor.read_text()):
            raise ValueError("qq_identity_bootstrap_missing")
        default_device_name(bytes.fromhex("12" * 16), native)
    if identity.qq_device_name_override != (identity.qq_device_identity_v1 and native == "8.3.2"):
        raise ValueError("qq_identity_native_capability_invalid")
    if OpenCC("t2s").convert("測試") != "测试":
        raise ValueError("bot_text_runtime_invalid")
    source = BytesIO()
    Image.new("RGBA", (64, 64), "#123456").save(source, format="PNG")
    result = render(
        Layout(engine="petpet", width=64, height=64, frame_duration_ms=50),
        RenderInputs(avatars=[source.getvalue()]),
        {},
    )
    image = Image.open(BytesIO(result.body))
    if (
        image.format != "GIF"
        or getattr(image, "n_frames", 0) < 2
        or image.info.get("duration") != 50
    ):
        raise ValueError("bot_render_proof_invalid")
    return {
        "role": role,
        "native_version": native,
        "runtime_version": identity.runtime_version,
        "render_proof": True,
        "qq_device_identity_v1": identity.qq_device_identity_v1,
        "qq_device_name_override": identity.qq_device_name_override,
        "qq_protocol_mode": identity.qq_protocol_mode,
        "qq_client_version": identity.qq_client_version,
        "pmhq_version": identity.pmhq_version,
        "qq_headed_v1": identity.qq_headed_v1,
    }


if __name__ == "__main__":
    role = os.environ.get("AL1S_MEME_ROLE", "")
    package = Path("/app/llbot/package.json" if role == "qq" else "/app/package.json")
    print(json.dumps(prove_runtime(Path("/opt/al1s/runtime-manifest.json"), package, role)))
