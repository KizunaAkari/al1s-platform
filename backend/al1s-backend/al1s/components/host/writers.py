"""Quiesce both registered Bot writers during shared storage maintenance."""

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
            "--format",
            "{{.ID}}",
        ]
    ).splitlines()
    if not identities:
        return None
    if len(identities) > 2:
        raise ValueError("component_writer_identity_ambiguous")
    rows = json.loads(deployment.runner.run(["docker", "inspect", *identities]))
    writers = []
    for row in rows:
        service = row["Config"].get("Labels", {}).get("com.docker.compose.service")
        validate_writer(row, service)
        writers.append(
            {
                "id": row["Id"],
                "image": row["Image"],
                "running": bool(row["State"]["Running"]),
                "service": service,
            }
        )
    return {"writers": writers}


def validate_writer(row: JsonObject, service: str) -> None:
    labels = row["Config"].get("Labels", {})
    if (
        service not in {"llbot", "discord-worker"}
        or row["Name"].removeprefix("/")
        not in (
            {"al1s-bot-qq"} if service == "llbot" else {"al1s-bot-discord", "al1s-discord-worker"}
        )
        or labels.get("com.docker.compose.project") != "al1s-bot"
        or labels.get("com.docker.compose.service") != service
    ):
        raise ValueError("component_writer_identity_changed")


def writer_action(deployment: ComposeDeployment, writer: JsonObject | None, action: str) -> None:
    if not writer:
        return
    if action not in {"start", "stop"}:
        raise ValueError("component_writer_action_invalid")
    writers = [value for value in writer.get("writers", [writer]) if value["running"]]
    if not writers:
        return
    rows = {
        row["Id"]: row
        for row in json.loads(
            deployment.runner.run(["docker", "inspect", *[value["id"] for value in writers]])
        )
    }
    for value in writers:
        row = rows[value["id"]]
        validate_writer(row, value.get("service", "discord-worker"))
        if row["Image"] != value["image"]:
            raise ValueError("component_writer_identity_changed")
    for value in writers:
        args = (
            ["docker", "stop", "--time", "75", value["id"]]
            if action == "stop"
            else ["docker", "start", value["id"]]
        )
        deployment.runner.run(args, 90 if action == "stop" else 30)
