import hashlib
import json
from uuid import UUID

from al1s.components.bot_contracts import BuiltQqCandidate
from al1s.components.contracts import ComponentRelease
from al1s.components.host.bot_proof import probe_bot_image
from al1s.components.host.commands import CommandRunner
from al1s.components.host.config import HostSettings
from al1s.components.host.journal import HostJournal
from al1s.components.host.platform_client import PlatformComponentClient
from al1s.components.qq_compatibility import assert_qq_source_verified


class QqCandidateBuilder:
    def __init__(
        self, settings: HostSettings, client: PlatformComponentClient, runner: CommandRunner
    ):
        self.settings, self.client, self.runner = settings, client, runner

    def prepare(self, identity: UUID) -> BuiltQqCandidate:
        release = self.client.release(identity)
        assert_qq_source_verified(release.native_version, release.registry_digest)
        if (
            release.component != "qq-bot"
            or release.source_kind != "registry"
            or release.state != "verifying"
            or release.runtime_artifact_digest != self.settings.qq_runtime_artifact_digest
        ):
            raise ValueError("qq_candidate_binding_invalid")
        root = self.settings.state_dir / "qq-candidates" / str(identity)
        journal = HostJournal(root)
        with journal.exclusive():
            previous = journal.load(identity)
            if previous is not None:
                if previous.get("state") == "completed":
                    return BuiltQqCandidate.model_validate(previous["result"])
                if previous.get("state") not in {"failed", "preparing"}:
                    raise ValueError("qq_candidate_preparation_unresolved")
            journal.save(identity, {"state": "preparing"})
            try:
                result = self.build(release)
            except Exception:
                journal.save(identity, {"state": "failed"})
                raise
            journal.save(identity, {"state": "completed", "result": result.model_dump()})
            return result

    def build(self, release: ComponentRelease) -> BuiltQqCandidate:
        assert (
            release.platform_manifest_digest
            and release.runtime_artifact_digest
            and release.runtime_version
        )
        artifact = json.loads(
            self.runner.run(["docker", "image", "inspect", release.runtime_artifact_digest])
        )[0]
        labels = artifact["Config"].get("Labels") or {}
        if (
            artifact["Id"] != release.runtime_artifact_digest
            or artifact["Architecture"] != release.architecture
            or labels.get("org.al1s.bot-role") != "qq"
            or labels.get("org.al1s.runtime-version") != release.runtime_version
        ):
            raise ValueError("qq_candidate_runtime_invalid")
        if labels.get("org.al1s.qq-protocol-mode") == "pmhq":
            proof = probe_bot_image(self.runner, artifact["Id"])
            if (release.native_version != "8.3.2" or proof.native_version != release.native_version
                    or proof.runtime_version != release.runtime_version
                    or proof.qq_protocol_mode != "pmhq" or not proof.qq_headed_v1
                    or (release.runtime_version == "2.1.0" and not proof.qq_quant_v1)):
                raise ValueError("qq_headed_candidate_unverified")
            # The frozen artifact already embeds the reviewed glibc native package.
            # The official musl registry image is discovery metadata, never copied.
            image = f"al1s-plachta:qq-headed-{release.native_version}-{artifact['Id'][7:23]}"
            self.runner.run(["docker", "tag", artifact["Id"], image])
            return BuiltQqCandidate(candidate_image=image, expected_image_id=artifact["Id"],
                                    native_version=proof.native_version,
                                    runtime_version=proof.runtime_version, proof=True)
        runtime = "al1s-plachta:qq-runtime-" + release.runtime_artifact_digest[7:23]
        self.runner.run(["docker", "tag", release.runtime_artifact_digest, runtime])
        source = "linyuchen/llbot@" + release.platform_manifest_digest
        self.runner.run(
            ["docker", "pull", "--platform", "linux/" + release.architecture, source], 600
        )
        key = hashlib.sha256(
            (release.platform_manifest_digest + release.runtime_artifact_digest).encode()
        ).hexdigest()[:16]
        image = f"al1s-plachta:qq-{release.native_version}-{key}"
        recipe = self.settings.deployment_root / "bots/Dockerfile.qq-candidate"
        self.runner.run(
            [
                "docker",
                "build",
                "--builder",
                "default",
                "--platform",
                "linux/" + release.architecture,
                "--build-arg",
                "LLBOT_BASE_IMAGE=" + source,
                "--build-arg",
                "RUNTIME_ARTIFACT=" + runtime,
                "--build-arg",
                "NATIVE_VERSION=" + release.native_version,
                "--build-arg",
                "RUNTIME_VERSION=" + release.runtime_version,
                "-f",
                str(recipe),
                "-t",
                image,
                str(recipe.parent),
            ],
            1200,
        )
        value = json.loads(self.runner.run(["docker", "image", "inspect", image]))[0]
        labels = value["Config"].get("Labels") or {}
        if (
            value["Architecture"] != release.architecture
            or labels.get("org.al1s.bot-role") != "qq"
            or labels.get("org.al1s.native-version") != release.native_version
            or labels.get("org.al1s.runtime-version") != release.runtime_version
        ):
            raise ValueError("qq_candidate_proof_invalid")
        proof = probe_bot_image(self.runner, value["Id"])
        if (
            proof.role != "qq"
            or proof.native_version != release.native_version
            or proof.runtime_version != release.runtime_version
        ):
            raise ValueError("qq_candidate_proof_invalid")
        return BuiltQqCandidate(
            candidate_image=image,
            expected_image_id=value["Id"],
            native_version=release.native_version,
            runtime_version=release.runtime_version,
            proof=True,
        )
