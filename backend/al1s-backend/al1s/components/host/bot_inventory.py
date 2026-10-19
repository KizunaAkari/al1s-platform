import json
from datetime import UTC, datetime

from al1s.components.host.bot_deployment import NAMES, BotDeployment
from al1s.components.host.bot_proof import RuntimeProof, probe_bot_image
from al1s.components.host.qq_identity_summary import read_qq_identity_summary


def bot_inventory(deployment: BotDeployment) -> list[dict[str, object]]:
    result = []
    try:
        observed = deployment.inspect_all()
        ids = sorted({row["Image"] for row in observed.values()})
        images = {
            row["Id"]: row
            for row in json.loads(deployment.runner.run(["docker", "image", "inspect", *ids]))
        }
    except Exception:
        observed, images = {}, {}
    for alias in ("qq", "discord"):
        try:
            value, image = observed[alias], images[observed[alias]["Image"]]
            proof = (
                RuntimeProof.model_validate(
                    json.loads(
                        deployment.runner.run(
                            [
                                "docker",
                                "exec",
                                NAMES[alias],
                                "python",
                                "-m",
                                "al1s.meme_workshop.runtime_probe",
                            ]
                        )
                    )
                )
                if value["State"]["Running"]
                else probe_bot_image(deployment.runner, value["Image"])
            )
            if proof.role != alias:
                raise ValueError("bot_inventory_role_invalid")
            status = (
                "offline"
                if not value["State"]["Running"]
                else (
                    "healthy"
                    if value["State"].get("Health", {}).get("Status") == "healthy"
                    else "unhealthy"
                )
            )
            result.append(
                dict(
                    component=alias + "-bot",
                    version=proof.native_version,
                    runtime_version=proof.runtime_version,
                    qq_protocol_mode=proof.qq_protocol_mode,
                    qq_client_version=proof.qq_client_version,
                    pmhq_version=proof.pmhq_version,
                    qq_identity=read_qq_identity_summary(deployment).model_dump()
                    if alias == "qq" and proof.qq_device_identity_v1 else None,
                    architecture=image["Architecture"],
                    status=status,
                    observed_at=datetime.now(UTC).isoformat(),
                    image=value["Config"]["Image"],
                    image_id=value["Image"],
                    source_identity=deployment.fingerprint(alias, value, deployment.config(alias)),
                )
            )
        except Exception:
            result.append(
                dict(
                    component=alias + "-bot",
                    version=None,
                    runtime_version=None,
                    architecture=None,
                    status="unknown",
                    observed_at=datetime.now(UTC).isoformat(),
                )
            )
    return result
