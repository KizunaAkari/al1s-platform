import copy
import hashlib
import json
import os
from typing import cast
from uuid import uuid4

from al1s.components.host.commands import CommandRunner
from al1s.components.host.config import HostSettings
from al1s.components.host.contracts import JsonObject

SERVICES = {
    "platform": ("backend",),
    "postgresql": ("postgres",),
    "s3": ("seaweed-master", "seaweed-volume", "seaweed-filer", "seaweed-s3"),
    "mqtt": ("mosquitto",),
}
SELECTORS = {
    "platform": "AL1S_COMPONENT_PLATFORM_IMAGE",
    "postgresql": "AL1S_COMPONENT_POSTGRES_IMAGE",
    "s3": "AL1S_COMPONENT_S3_IMAGE",
    "mqtt": "AL1S_COMPONENT_MQTT_IMAGE",
}


class ComposeDeployment:
    def __init__(self, settings: HostSettings, runner: CommandRunner):
        self.settings, self.runner = settings, runner

    def config(self) -> JsonObject:
        result = json.loads(
            self.runner.run([*self.settings.compose_arguments(), "config", "--format", "json"])
        )
        if result.get("name") != self.settings.project:
            raise ValueError("component_project_changed")
        return cast(JsonObject, result)

    def inspect(self, service: str) -> JsonObject:
        config = self.config()
        name = config["services"][service].get("container_name")
        if not isinstance(name, str) or not name.startswith(self.settings.container_name_prefix):
            raise ValueError("component_container_name_changed")
        result = json.loads(self.runner.run(["docker", "inspect", name]))[0]
        labels = result["Config"].get("Labels", {})
        if (
            labels.get("com.docker.compose.project") != self.settings.project
            or labels.get("com.docker.compose.service") != service
        ):
            raise ValueError("component_container_identity_changed")
        return cast(JsonObject, result)

    def source_identity(self, component: str) -> str:
        config = self.config()
        return self.fingerprint(
            config, {key: self.inspect(key) for key in SERVICES[component]}, component
        )

    @staticmethod
    def fingerprint(config: JsonObject, rows: dict[str, JsonObject], component: str) -> str:
        fingerprint = [
            {
                "service": key,
                "id": (row := rows[key])["Id"],
                "image": row["Image"],
                "config": row["Config"]["Labels"].get("com.docker.compose.config-hash"),
            }
            for key in SERVICES[component]
        ]
        fingerprint.append(
            {
                "effective_config_hash": hashlib.sha256(
                    json.dumps(config, sort_keys=True).encode()
                ).hexdigest()
            }
        )
        return hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()

    def observation(self) -> tuple[JsonObject, dict[str, JsonObject]]:
        config = self.config()
        expected = {
            service: config["services"][service]["container_name"]
            for group in SERVICES.values()
            for service in group
        }
        if any(
            not name.startswith(self.settings.container_name_prefix) for name in expected.values()
        ):
            raise ValueError("component_container_name_changed")
        available = self.runner.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=" + self.settings.project,
                "--format",
                "{{.Names}}",
            ]
        ).splitlines()
        names = [name for name in expected.values() if name in available]
        observed = json.loads(self.runner.run(["docker", "inspect", *names])) if names else []
        rows = {}
        for row in observed:
            labels = row["Config"].get("Labels", {})
            service = labels.get("com.docker.compose.service")
            if labels.get("com.docker.compose.project") != self.settings.project or expected.get(
                service
            ) != row["Name"].removeprefix("/"):
                raise ValueError("component_container_identity_changed")
            rows[service] = row
        return config, rows

    def selectors(self) -> dict[str, str]:
        result = {}
        for line in self.settings.selectors_file.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if (
                key not in SELECTORS.values()
                or not value.strip()
                or any(c.isspace() for c in value)
            ):
                raise ValueError("component_selector_invalid")
            result[key] = value
        return cast(JsonObject, result)

    def write_selectors(self, values: dict[str, str]) -> None:
        if set(values) != set(SELECTORS.values()):
            raise ValueError("component_selector_incomplete")
        temporary = self.settings.selectors_file.with_name(".components-" + str(uuid4()) + ".tmp")
        temporary.write_text(
            "".join(f"{key}={values[key]}\n" for key in sorted(values)), encoding="utf-8"
        )
        os.replace(temporary, self.settings.selectors_file)

    def change_image(self, component: str, image: str) -> None:
        baseline = self.config()
        original = self.selectors()
        changed = {**original, SELECTORS[component]: image}
        self.write_selectors(changed)
        try:
            expected = copy.deepcopy(baseline)
            for service in SERVICES[component]:
                expected["services"][service]["image"] = image
            if component == "platform":
                for name in ("migrate", "s3-init", "log-collector"):
                    if name in expected["services"]:
                        expected["services"][name]["image"] = image
            if self.config() != expected:
                raise ValueError("component_unexpected_configuration_change")
        except Exception:
            self.write_selectors(original)
            raise

    def stop(self, services: tuple[str, ...]) -> None:
        self.runner.run(
            [*self.settings.compose_arguments(), "stop", "--timeout", "390", *services], 450
        )

    def start(self, services: tuple[str, ...]) -> None:
        self.runner.run(
            [
                *self.settings.compose_arguments(),
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--pull",
                "never",
                *services,
            ],
            180,
        )
