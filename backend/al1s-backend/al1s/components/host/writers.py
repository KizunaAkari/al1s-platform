"""Only the registered Discord meme writer is quiesced for an S3 switch."""

import json

from al1s.components.host.compose_driver import ComposeDeployment
from al1s.components.host.contracts import JsonObject


def capture_writer(deployment: ComposeDeployment) -> JsonObject | None:
    if deployment.settings.project != "al1s-platform":
        return None
    identities = deployment.runner.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project=al1s-bot",
            "--filter",
            "label=com.docker.compose.service=discord-worker",
            "--format",
            "{{.ID}}",
        ]
    ).splitlines()
    if not identities:
        return None
    if len(identities) != 1:
        raise ValueError("component_writer_identity_ambiguous")
    row = json.loads(deployment.runner.run(["docker", "inspect", identities[0]]))[0]
    labels = row["Config"].get("Labels", {})
    if (
        row["Name"].removeprefix("/") not in {"al1s-bot-discord", "al1s-discord-worker"}
        or labels.get("com.docker.compose.project") != "al1s-bot"
        or labels.get("com.docker.compose.service") != "discord-worker"
    ):
        raise ValueError("component_writer_identity_changed")
    return {"id": row["Id"], "image": row["Image"], "running": bool(row["State"]["Running"])}


def writer_action(deployment: ComposeDeployment, writer: JsonObject | None, action: str) -> None:
    if not writer or not writer["running"]:
        return
    row = json.loads(deployment.runner.run(["docker", "inspect", writer["id"]]))[0]
    labels = row["Config"].get("Labels", {})
    if (
        row["Id"] != writer["id"]
        or row["Image"] != writer["image"]
        or labels.get("com.docker.compose.project") != "al1s-bot"
        or labels.get("com.docker.compose.service") != "discord-worker"
    ):
        raise ValueError("component_writer_identity_changed")
    if action == "stop":
        deployment.runner.run(["docker", "stop", "--time", "75", writer["id"]], 90)
    elif action == "start":
        deployment.runner.run(["docker", "start", writer["id"]], 30)
    else:
        raise ValueError("component_writer_action_invalid")
