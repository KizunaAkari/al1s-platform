import hashlib
import json
import os
import tarfile
import time
from pathlib import Path
from typing import cast
from uuid import UUID

from al1s.components.bot_contracts import BotAlias
from al1s.components.contracts import ComponentOperation, ComponentRelease
from al1s.components.host.backup import private_json
from al1s.components.host.bot_deployment import NAMES, SELECTORS, BotDeployment
from al1s.components.host.bot_environment import normalize_image_metadata
from al1s.components.host.bot_mounts import same_protection
from al1s.components.host.bot_proof import RuntimeProof, probe_bot_image
from al1s.components.host.contracts import JsonObject
from al1s.components.host.platform_client import PlatformComponentClient
from al1s.components.host.qq_identity_summary import read_qq_identity_summary
from al1s.components.qq_compatibility import assert_qq_source_verified, compatible_qq_sources
from al1s.meme_workshop.operation_identity import OperationIdentity, stored_identity

DATA = {"qq": "/app/llbot/data", "discord": "/var/lib/al1s-plachta"}


def same_device_configuration(before: OperationIdentity, observed: OperationIdentity) -> bool:
    source = before.qq_identity
    if source is None or not source.configured:
        return True
    return (observed.qq_identity == source and not source.pending and not source.failed)


def protected_state(value: JsonObject) -> JsonObject:
    return {
        "env": sorted(value["Config"]["Env"]),
        "user": value["Config"].get("User"),
        "mounts": sorted(value["Mounts"], key=lambda mount: mount["Destination"]),
        "ports": value["HostConfig"].get("PortBindings"),
        "networks": sorted(value["NetworkSettings"]["Networks"]),
    }


def backup_identity(path: Path) -> tuple[int, str]:
    size, digest = path.stat().st_size, hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024**2), b""):
            digest.update(chunk)
    with tarfile.open(path, "r|") as archive:
        for member in archive:
            if member.offset_data + member.size > size:
                raise ValueError("bot_backup_truncated")
    return size, digest.hexdigest()


class BotUpgradeDriver:
    def __init__(self, deployment: BotDeployment, client: PlatformComponentClient, state_dir: Path):
        self.deployment, self.client = deployment, client
        self.root = state_dir / "bot-deployments"

    def directory(self, identity: UUID) -> Path:
        return self.root / str(UUID(str(identity)))

    def payload(self, payload: JsonObject) -> tuple[ComponentOperation, ComponentRelease, BotAlias]:
        operation = ComponentOperation.model_validate(payload["operation"])
        release = ComponentRelease.model_validate(payload["release"])
        alias = operation.target.bot_alias
        if (
            operation.target.kind != "bot"
            or alias is None
            or operation.component != f"{alias}-bot"
            or release.component != operation.component
            or release.release_id != operation.release_id
            or release.state != "published"
        ):
            raise ValueError("bot_upgrade_binding_invalid")
        return operation, release, alias

    def prepare(self, identity: UUID, payload: JsonObject) -> JsonObject:
        operation, release, alias = self.payload(payload)
        if (
            operation.operation_id != identity
            or self.deployment.source_identity(alias) != operation.expected_source
        ):
            raise ValueError("bot_source_changed")
        directory = self.directory(identity)
        directory.mkdir(parents=True, mode=0o700)
        before = self.deployment.inspect(alias)
        other: BotAlias = "discord" if alias == "qq" else "qq"
        peer = self.deployment.inspect(other)
        if release.source_kind == "archive":
            path = directory / "candidate.tar"
            self.client.download(release, path)
            self.deployment.runner.run(["docker", "load", "-i", str(path)], 600)
        assert release.candidate_image and release.expected_image_id
        image = json.loads(
            self.deployment.runner.run(["docker", "image", "inspect", release.candidate_image])
        )[0]
        if (
            image["Id"] != release.expected_image_id
            or image["Architecture"] != release.architecture
            or (image["Config"].get("Labels") or {}).get("org.al1s.bot-role") != alias
        ):
            raise ValueError("bot_candidate_identity_invalid")
        proof = probe_bot_image(self.deployment.runner, image["Id"])
        original = probe_bot_image(self.deployment.runner, before["Image"])
        if alias == "qq" and original.qq_quant_v1 and not proof.qq_quant_v1:
            raise ValueError("qq_quant_runtime_required")
        if (alias == "qq" and original.qq_protocol_mode == "pmhq" and (
                proof.qq_protocol_mode != "pmhq" or not proof.qq_headed_v1
                    or proof.qq_client_version != original.qq_client_version
                    or proof.pmhq_version != original.pmhq_version)):
            raise ValueError("qq_headed_data_format_unverified")
        if proof.qq_headed_v1 and image["Config"].get("Entrypoint") != [
            "python", "-m", "al1s.bots.qq_headed.startup"
        ]:
            raise ValueError("qq_headed_bootstrap_missing")
        if alias == "qq" and original.qq_device_identity_v1:
            summary = read_qq_identity_summary(self.deployment)
            if summary.pending or summary.failed:
                raise ValueError("qq_identity_pending_or_unresolved")
            if summary.configured and not proof.qq_device_identity_v1:
                raise ValueError("qq_identity_runtime_unsupported")
            if summary.custom_name and not proof.qq_device_name_override:
                raise ValueError("qq_device_name_override_unsupported")
        if proof.qq_device_identity_v1 and image["Config"].get("Entrypoint") != [
            "python", "-m", "al1s.bots.qq_identity.startup"
        ]:
            raise ValueError("qq_identity_bootstrap_missing")
        operational = (
            self.live_identity(alias)
            if before["State"]["Running"]
            else self.archived_identity(alias, identity, before)
        )
        if operational.role != alias:
            raise ValueError("bot_source_identity_mismatch")
        if alias == "qq":
            assert_qq_source_verified(release.native_version, release.registry_digest)
            if original.native_version not in compatible_qq_sources(release.native_version).split(
                ","
            ):
                raise ValueError("qq_data_format_unverified")
        if (
            proof.role != alias
            or proof.native_version != release.native_version
            or (release.runtime_version and proof.runtime_version != release.runtime_version)
            or original.role != alias
            or original.native_version.split(".")[0] != release.native_version.split(".")[0]
        ):
            raise ValueError("bot_candidate_capability_invalid")
        selectors = self.deployment.selectors()
        if selectors[SELECTORS[alias]] != before["Config"]["Image"]:
            raise ValueError("bot_selector_source_mismatch")
        snapshot = {
            "alias": alias,
            "before_image": before["Config"]["Image"],
            "before_id": before["Image"],
            "running": before["State"]["Running"],
            "selectors": selectors,
            "peer": other,
            "peer_id": peer["Id"],
            "peer_started": peer["State"]["StartedAt"],
            "candidate_id": image["Id"],
            "data_format": release.data_policy,
            "protected": protected_state(before),
            "before_proof": original.model_dump(),
            "operational_identity": operational.model_dump(),
            "phase": "prepared",
        }
        private_json(directory / "snapshot.json", snapshot)
        return {"backup_id": str(identity)}

    def switch(self, identity: UUID, payload: JsonObject, point: JsonObject) -> None:
        operation, release, alias = self.payload(payload)
        snapshot = self.snapshot(identity)
        if self.deployment.source_identity(alias) != operation.expected_source:
            raise ValueError("bot_source_changed")
        self.deployment.stop(alias)
        if self.deployment.inspect(alias)["State"]["Running"]:
            raise ValueError("bot_not_quiesced")
        snapshot["phase"] = "quiesced"
        private_json(self.directory(identity) / "snapshot.json", snapshot)
        backup = self.directory(identity) / "data.tar"
        if not backup.exists():
            self.deployment.runner.stream(
                ["docker", "cp", f"{NAMES[alias]}:{DATA[alias]}/.", "-"], backup, 600
            )
        os.chmod(backup, 0o600)
        size, digest = backup_identity(backup)
        snapshot.update(data_backup_sha256=digest, data_backup_bytes=size, phase="backed_up")
        private_json(self.directory(identity) / "snapshot.json", snapshot)
        assert release.candidate_image
        self.deployment.change_image(alias, release.candidate_image)
        self.deployment.start(alias)

    def verify(self, identity: UUID, payload: JsonObject, point: JsonObject) -> bool:
        _, release, alias = self.payload(payload)
        snapshot = self.snapshot(identity)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = self.deployment.inspect(alias)
            if state["Image"] != release.expected_image_id:
                return False
            if state["State"].get("Health", {}).get("Status") == "healthy":
                if not self.protected_matches(state, snapshot):
                    return False
                proof = self.live_proof(alias)
                observed_identity = self.live_identity(alias)
                before_identity = OperationIdentity.model_validate(snapshot["operational_identity"])
                return (
                    proof.role == alias
                    and observed_identity.role == alias
                    and observed_identity.binding == before_identity.binding
                    and same_device_configuration(before_identity, observed_identity)
                    and (before_identity.qq_identity is None
                         or not before_identity.qq_identity.configured
                         or (proof.qq_device_identity_v1
                             and (not before_identity.qq_identity.custom_name
                                  or proof.qq_device_name_override)))
                    and proof.native_version == release.native_version
                    and (
                        not release.runtime_version
                        or proof.runtime_version == release.runtime_version
                    )
                )
            time.sleep(2)
        return False

    def snapshot(self, identity: UUID) -> JsonObject:
        path = self.directory(identity) / "snapshot.json"
        if path.stat().st_size > 1024**2:
            raise ValueError("bot_snapshot_oversized")
        return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))

    def live_proof(self, alias: BotAlias) -> RuntimeProof:
        output = self.deployment.runner.run(
            ["docker", "exec", NAMES[alias], "python", "-m", "al1s.meme_workshop.runtime_probe"], 30
        )
        return RuntimeProof.model_validate(json.loads(output))

    def live_identity(self, alias: BotAlias) -> OperationIdentity:
        output = self.deployment.runner.run(
            [
                "docker",
                "exec",
                NAMES[alias],
                "python",
                "-m",
                "al1s.meme_workshop.operation_identity",
            ],
            30,
        )
        return OperationIdentity.model_validate(json.loads(output))

    def identity_matches(
        self, alias: BotAlias, identity: UUID, snapshot: JsonObject, state: JsonObject
    ) -> bool:
        observed = (
            self.live_identity(alias)
            if state["State"]["Running"]
            else self.archived_identity(alias, identity, state)
        )
        source = OperationIdentity.model_validate(snapshot["operational_identity"])
        return (observed.role == source.role == alias and observed.binding == source.binding
                and same_device_configuration(source, observed))

    def archived_identity(
        self, alias: BotAlias, identity: UUID, state: JsonObject
    ) -> OperationIdentity:
        directory = self.directory(identity)
        env = dict(value.split("=", 1) for value in state["Config"]["Env"])
        account = env.get("AUTO_LOGIN_QQ", "")
        if alias == "qq" and (not account.isdecimal() or not 1 <= len(account) <= 20):
            raise ValueError("qq_identity_unconfigured")
        names = (
            {"machine_guid.bin", f"config_{account}.json"}
            if alias == "qq"
            else {"credential", "last-known-good.json"}
        )
        headed = alias == "qq" and env.get("PROTOCOL_MODE") == "pmhq"
        if headed:
            names = {"qq-client/.al1s-client-profile.json", "qq-client/versions/config.json"}
        from al1s.components.operations import identity_runtime_version_supported
        if (alias == "qq" and not headed and identity_runtime_version_supported(
                (state["Config"].get("Labels") or {}).get("org.al1s.runtime-version"))):
            from al1s.bots.qq_identity.repository import RECORD_NAME
            names.add(RECORD_NAME)
        root = directory / "identity-files"
        root.mkdir(mode=0o700, exist_ok=True)
        from uuid import uuid4

        for name in sorted(names):
            archive = root / (str(uuid4()) + ".tar")
            self.deployment.runner.stream(
                ["docker", "cp", f"{NAMES[alias]}:{DATA[alias]}/{name}", "-"], archive, 30
            )
            try:
                with tarfile.open(archive, "r|") as data:
                    member = data.next()
                    if (
                        member is None
                        or member.name.removeprefix("./") != Path(name).name
                        or not member.isfile()
                        or member.size > 128 * 1024
                    ):
                        raise ValueError("bot_identity_archive_invalid")
                    source = data.extractfile(member)
                    if source is None:
                        raise ValueError("bot_identity_archive_invalid")
                    path = root / name
                    path.parent.mkdir(mode=0o700, exist_ok=True)
                    with path.open("wb") as output:
                        os.chmod(path, 0o600)
                        output.write(source.read(128 * 1024 + 1))
            finally:
                archive.unlink(missing_ok=True)
        if headed:
            from al1s.bots.qq_headed.profile import validate_client_version
            proof = probe_bot_image(self.deployment.runner, state["Image"])
            validate_client_version(root, proof.qq_client_version or "")
        return stored_identity(alias, env, root)

    def recover(self, identity: UUID, payload: JsonObject, point: JsonObject) -> str:
        _, _release, alias = self.payload(payload)
        snapshot = self.snapshot(identity)
        if snapshot["alias"] != alias:
            return "unknown"
        state = self.deployment.inspect(alias)
        if (
            state["Image"] == snapshot["candidate_id"]
            and state["State"].get("Health", {}).get("Status") == "healthy"
        ):
            return "succeeded" if self.verify(identity, payload, point) else "unknown"
        if state["Image"] not in {
            snapshot["before_id"],
            snapshot["candidate_id"],
        } or not self.protected_matches(state, snapshot):
            return "unknown"
        if (
            state["Image"] == snapshot["before_id"]
            and state["State"]["Running"] == snapshot["running"]
            and (
                not snapshot["running"]
                or (
                    state["State"].get("Health", {}).get("Status") == "healthy"
                    and self.live_proof(alias).model_dump()
                    == RuntimeProof.model_validate(snapshot["before_proof"]).model_dump()
                )
            )
        ):
            return (
                "failed_safe"
                if self.identity_matches(alias, identity, snapshot, state)
                and self.repair_selector(alias, snapshot)
                else "unknown"
            )
        if state["Image"] == snapshot["candidate_id"]:
            if snapshot["data_format"] != "same-format" or snapshot.get("phase") != "backed_up":
                return "unknown"
            size, digest = backup_identity(self.directory(identity) / "data.tar")
            if (size, digest) != (snapshot["data_backup_bytes"], snapshot["data_backup_sha256"]):
                return "unknown"
        original = json.loads(
            self.deployment.runner.run(["docker", "image", "inspect", snapshot["before_image"]])
        )[0]
        if original["Id"] != snapshot["before_id"]:
            return "unknown"
        self.deployment.stop(alias)
        if self.deployment.inspect(alias)["State"]["Running"]:
            return "unknown"
        self.deployment.change_image(alias, snapshot["before_image"])
        self.deployment.restore(alias, bool(snapshot["running"]))
        restored = self.deployment.inspect(alias)
        if (
            restored["Image"] != snapshot["before_id"]
            or not self.protected_matches(restored, snapshot)
            or restored["State"]["Running"] != snapshot["running"]
        ):
            return "unknown"
        if snapshot["running"] and (
            restored["State"].get("Health", {}).get("Status") != "healthy"
            or self.live_proof(alias).model_dump()
            != RuntimeProof.model_validate(snapshot["before_proof"]).model_dump()
        ):
            return "unknown"
        if not self.identity_matches(alias, identity, snapshot, restored):
            return "unknown"
        return "failed_safe"

    def protected_matches(self, state: JsonObject, snapshot: JsonObject) -> bool:
        return same_protection(
            normalize_image_metadata(self.deployment, state, snapshot, protected_state(state)),
            snapshot["protected"], self.deployment.settings.deployment_root
        )

    def repair_selector(self, alias: BotAlias, snapshot: JsonObject) -> bool:
        if self.deployment.selectors()[SELECTORS[alias]] == snapshot["before_image"]:
            return True
        original = json.loads(
            self.deployment.runner.run(["docker", "image", "inspect", snapshot["before_image"]])
        )[0]
        if original["Id"] != snapshot["before_id"]:
            return False
        self.deployment.change_image(alias, snapshot["before_image"])
        return bool(self.deployment.selectors()[SELECTORS[alias]] == snapshot["before_image"])
