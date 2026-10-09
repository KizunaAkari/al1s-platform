import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from al1s.components.host.compose_driver import ComposeDeployment
from al1s.components.host.contracts import JsonObject
from al1s.components.host.layout import service_map, storage_layout

VERSION_COMMANDS = {
    "maa": ["python", "-c", "from maa.library import Library;print(Library.version())"],
    "platform": [
        "python",
        "-c",
        "import os;from al1s.app.config import Settings;"
        "print(os.environ.get('AL1S_SERVICE_VERSION',Settings.model_fields['service_version'].default))",
    ],
    "postgresql": ["postgres", "--version"],
    "s3": ["weed", "version"],
    "mqtt": ["/usr/sbin/mosquitto", "-h"],
    "linux-terminal": [
        "python",
        "-c",
        "from importlib.metadata import version;print(version('al1s-terminal'))",
    ],
}


def parsed_version(component: str, output: str) -> str:
    if component in {"platform", "linux-terminal", "maa"}:
        value = output.strip()
    elif component == "s3":
        matched = re.search(r"version\s+\S+\s+(\d+(?:\.\d+)+)", output)
        value = matched.group(1) if matched else ""
    else:
        matched = re.search(r"(?:PostgreSQL\)\s*|version\s+)(\d+(?:\.\d+)+)", output)
        value = matched.group(1) if matched else ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", value):
        raise ValueError("component_native_version_unknown")
    return value


def inventory(deployment: ComposeDeployment) -> list[JsonObject]:
    result = []
    config, observed = deployment.observation()
    ids = list({row["Image"] for row in observed.values()})
    images = (
        {
            row["Id"]: row
            for row in json.loads(deployment.runner.run(["docker", "image", "inspect", *ids]))
        }
        if ids
        else {}
    )
    for component, services in service_map(config).items():
        try:
            rows = [observed[service] for service in services]
            primary = rows[-1] if component == "s3" else rows[0]
            output = deployment.runner.run(
                ["docker", "exec", primary["Id"], *VERSION_COMMANDS[component]]
            )
            ready = all(
                row["State"].get("Running")
                and row["State"].get("Health", {}).get("Status", "healthy") == "healthy"
                for row in rows
            )
            image = images[primary["Image"]]
            result.append(
                {
                    "component": component,
                    "version": parsed_version(component, output),
                    "architecture": image["Architecture"],
                    "status": "healthy" if ready else "unhealthy",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "image": primary["Config"]["Image"],
                    "image_id": primary["Image"],
                    "source_identity": deployment.fingerprint(config, observed, component),
                    "storage_layout": storage_layout(config) if component == "s3" else None,
                }
            )
        except Exception:
            result.append(
                {
                    "component": component,
                    "version": None,
                    "architecture": None,
                    "status": "unknown",
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            )
    return result


def probe_image(deployment: ComposeDeployment, component: str, image_id: str) -> str:
    name = "al1s-component-probe-" + uuid4().hex[:12]
    command = VERSION_COMMANDS[component]
    try:
        output = deployment.runner.run(
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
                "--tmpfs",
                "/tmp",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--pids-limit",
                "64",
                "--entrypoint",
                command[0],
                image_id,
                *command[1:],
            ],
            60,
        )
        return parsed_version(component, output)
    finally:
        # The generated name is exclusive to this one disposable probe, never a runtime service.
        with suppress(Exception):
            deployment.runner.run(["docker", "rm", "-f", name], 15)


def probe_storage_layout(deployment: ComposeDeployment, image_id: str) -> None:
    output = deployment.runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--entrypoint",
            "python",
            image_id,
            "/app/storage-layout-probe.py",
        ],
        60,
    )
    result = json.loads(output)
    if result != {
        "layout": "seaweed-four-process-v1",
        "roles": ["master", "volume", "filer", "s3"],
    }:
        raise ValueError("component_storage_layout_probe_failed")
