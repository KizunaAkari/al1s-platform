import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Self, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.components.bot_contracts import BotAlias
from al1s.components.host.commands import CommandRunner
from al1s.components.host.contracts import JsonObject
from al1s.components.host.journal import HostJournal

SERVICES = {"qq": "llbot", "discord": "discord-worker"}
NAMES = {"qq": "al1s-bot-qq", "discord": "al1s-bot-discord"}
SELECTORS = {"qq": "AL1S_QQ_BOT_IMAGE", "discord": "AL1S_DISCORD_WORKER_IMAGE"}


class BotRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    compose_files: list[Path] = Field(min_length=1, max_length=16)
    env_files: list[Path] = Field(min_length=1, max_length=16)


class BotDeploymentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    deployment_root: Path
    selectors_file: Path
    registrations: dict[BotAlias, BotRegistration]

    @model_validator(mode="after")
    def registered(self) -> Self:
        root = self.deployment_root.resolve()
        if set(self.registrations) != {"qq", "discord"}:
            raise ValueError("both_bot_deployments_required")
        paths = [self.selectors_file]
        for item in self.registrations.values():
            paths.extend([*item.compose_files, *item.env_files])
        if any(not p.resolve().is_relative_to(root) for p in paths):
            raise ValueError("bot_deployment_path_outside_root")
        return self


class BotDeployment:
    def __init__(self, settings: BotDeploymentSettings, runner: CommandRunner):
        self.settings, self.runner = settings, runner

    def args(self, alias: BotAlias) -> list[str]:
        item = self.settings.registrations[alias]
        args = ["docker", "compose", "-p", "al1s-bot"]
        for path in [*item.env_files, self.settings.selectors_file]:
            args.extend(["--env-file", str(path)])
        for path in item.compose_files:
            args.extend(["-f", str(path)])
        return args

    def config(self, alias: BotAlias) -> JsonObject:
        value = json.loads(self.runner.run([*self.args(alias), "config", "--format", "json"]))
        if value["services"][SERVICES[alias]]["container_name"] != NAMES[alias]:
            raise ValueError("bot_container_name_changed")
        return cast(JsonObject, value)

    def inspect(self, alias: BotAlias) -> JsonObject:
        value = json.loads(self.runner.run(["docker", "inspect", NAMES[alias]]))[0]
        labels = value["Config"].get("Labels", {})
        if (
            value["Name"].removeprefix("/") != NAMES[alias]
            or labels.get("com.docker.compose.project") != "al1s-bot"
            or labels.get("com.docker.compose.service") != SERVICES[alias]
        ):
            raise ValueError("bot_deployment_identity_changed")
        return cast(JsonObject, value)

    def source_identity(self, alias: BotAlias) -> str:
        value = self.inspect(alias)
        config = self.config(alias)
        return self.fingerprint(alias, value, config)

    def inspect_all(self) -> dict[BotAlias, JsonObject]:
        rows = json.loads(self.runner.run(["docker", "inspect", NAMES["qq"], NAMES["discord"]]))
        result: dict[BotAlias, JsonObject] = {}
        for alias in ("qq", "discord"):
            matches = [row for row in rows if row["Name"].removeprefix("/") == NAMES[alias]]
            if (
                len(matches) != 1
                or matches[0]["Config"].get("Labels", {}).get("com.docker.compose.project")
                != "al1s-bot"
                or matches[0]["Config"].get("Labels", {}).get("com.docker.compose.service")
                != SERVICES[alias]
            ):
                raise ValueError("bot_deployment_identity_changed")
            result[alias] = matches[0]
        return result

    def reject_candidate_tag_collision(self, image: str, image_id: str) -> None:
        for row in self.inspect_all().values():
            if row["Config"]["Image"].split("@")[0] == image and row["Image"] != image_id:
                raise ValueError("bot_active_image_tag_conflict")
        if image in self.selectors().values():
            selected = json.loads(self.runner.run(["docker", "image", "inspect", image]))[0]
            if selected["Id"] != image_id:
                raise ValueError("bot_selector_image_tag_conflict")

    @staticmethod
    def fingerprint(alias: BotAlias, value: JsonObject, config: JsonObject) -> str:
        service = config["services"][SERVICES[alias]]
        identity = {
            "container": value["Id"],
            "image": value["Image"],
            "service": service,
            "mounts": value["Mounts"],
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def selectors(self) -> dict[str, str]:
        values = {}
        for line in self.settings.selectors_file.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key not in SELECTORS.values() or not value or any(c.isspace() for c in value):
                raise ValueError("invalid_bot_selector")
            values[key] = value
        if set(values) != set(SELECTORS.values()):
            raise ValueError("incomplete_bot_selectors")
        return values

    def change_image(self, alias: BotAlias, image: str) -> None:
        with HostJournal(self.settings.selectors_file.parent / ".selector-state").exclusive():
            self._change_image(alias, image)

    def _change_image(self, alias: BotAlias, image: str) -> None:
        before = self.config(alias)
        original = self.selectors()
        changed = {**original, SELECTORS[alias]: image}
        self.write_selectors(changed)
        expected = copy.deepcopy(before)
        expected["services"][SERVICES[alias]]["image"] = image
        if self.config(alias) != expected:
            self.write_selectors(original)
            raise ValueError("unexpected_bot_compose_change")

    def write_selectors(self, values: dict[str, str]) -> None:
        if set(values) != set(SELECTORS.values()):
            raise ValueError("incomplete_bot_selectors")
        path = self.settings.selectors_file
        temporary = path.with_name(".bots-" + str(uuid4()) + ".tmp")
        temporary.write_text(
            "".join(f"{k}={values[k]}\n" for k in sorted(values)), encoding="utf-8"
        )
        os.replace(temporary, path)

    def stop(self, alias: BotAlias) -> None:
        self.runner.run([*self.args(alias), "stop", "-t", "30", SERVICES[alias]], 60)

    def start(self, alias: BotAlias) -> None:
        self.runner.run(
            [
                *self.args(alias),
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--pull",
                "never",
                SERVICES[alias],
            ],
            120,
        )

    def restore(self, alias: BotAlias, running: bool) -> None:
        if running:
            self.start(alias)
            return
        self.runner.run(
            [
                *self.args(alias),
                "create",
                "--force-recreate",
                "--no-deps",
                "--pull",
                "never",
                SERVICES[alias],
            ],
            120,
        )
