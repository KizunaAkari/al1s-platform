import json
from pathlib import Path
from uuid import UUID

from al1s.components.contracts import ComponentRelease
from al1s.components.host.bot_driver import BotUpgradeDriver
from al1s.components.host.bot_proof import probe_bot_image
from al1s.components.host.contracts import HostDriver, JsonObject
from al1s.components.host.deployment_driver import DeploymentDriver
from al1s.components.verification import DockerArchiveVerifier


class DeploymentRouter:
    def __init__(self, platform: DeploymentDriver, bot: BotUpgradeDriver | None):
        self.platform, self.bot = platform, bot

    def target(self, payload: JsonObject) -> HostDriver:
        if payload.get("operation", {}).get("target", {}).get("kind") == "bot":
            if self.bot is None:
                raise ValueError("bot_deployments_unconfigured")
            return self.bot
        return self.platform

    def prepare(self, identity: UUID, payload: JsonObject) -> JsonObject:
        return self.target(payload).prepare(identity, payload)

    def switch(self, identity: UUID, payload: JsonObject, point: JsonObject) -> None:
        return self.target(payload).switch(identity, payload, point)

    def verify(self, identity: UUID, payload: JsonObject, point: JsonObject) -> bool:
        return self.target(payload).verify(identity, payload, point)

    def recover(self, identity: UUID, payload: JsonObject, point: JsonObject) -> str:
        return self.target(payload).recover(identity, payload, point)

    def verify_archive(self, path: Path, release: ComponentRelease) -> None:
        if release.component not in {"qq-bot", "discord-bot"}:
            return self.platform.verify_archive(path, release)
        if self.bot is None:
            raise ValueError("bot_deployments_unconfigured")
        assert release.candidate_image and release.expected_image_id
        DockerArchiveVerifier(lambda *_: release.native_version).verify(path, release)
        self.bot.deployment.reject_candidate_tag_collision(
            release.candidate_image, release.expected_image_id
        )
        runner = self.bot.deployment.runner
        runner.run(["docker", "load", "-i", str(path)], 600)
        image = json.loads(runner.run(["docker", "image", "inspect", release.candidate_image]))[0]
        labels = image["Config"].get("Labels") or {}
        if (
            image["Id"] != release.expected_image_id
            or image["Architecture"] != release.architecture
            or labels.get("org.al1s.bot-role") != release.component[:-4]
            or labels.get("org.al1s.native-version") != release.native_version
        ):
            raise ValueError("bot_archive_identity_invalid")
        proof = probe_bot_image(runner, image["Id"])
        if proof.role != release.component[:-4] or proof.native_version != release.native_version:
            raise ValueError("bot_archive_capability_invalid")
