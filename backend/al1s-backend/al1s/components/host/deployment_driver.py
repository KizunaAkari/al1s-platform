import json
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from al1s.components.contracts import ComponentOperation, ComponentRelease
from al1s.components.host.backup import backup_data, checksum, private_json, schema_revision
from al1s.components.host.compose_driver import SERVICES, ComposeDeployment
from al1s.components.host.contracts import JsonObject
from al1s.components.host.drivers import mqtt, platform, postgresql, seaweedfs
from al1s.components.host.layout import storage_layout, validate_s3_image
from al1s.components.host.platform_client import PlatformComponentClient
from al1s.components.host.probes import inventory, probe_image, probe_storage_layout
from al1s.components.host.writers import capture_writer, writer_action
from al1s.components.verification import DockerArchiveVerifier

COMPATIBLE = {
    "postgresql": postgresql.compatible,
    "s3": seaweedfs.compatible,
    "mqtt": mqtt.compatible,
}


class DeploymentDriver:
    def __init__(self, deployment: ComposeDeployment, client: PlatformComponentClient):
        self.deployment, self.client = deployment, client
        self.root = deployment.settings.state_dir / "deployments"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, identity: UUID) -> Path:
        return self.root / str(UUID(str(identity)))

    def prepare(self, identity: UUID, payload: JsonObject) -> JsonObject:
        operation = ComponentOperation.model_validate(payload["operation"])
        release = ComponentRelease.model_validate(payload["release"])
        if (
            operation.operation_id != identity
            or operation.target.kind != "platform"
            or release.state != "published"
        ):
            raise ValueError("component_operation_binding")
        if operation.release_id != release.release_id or operation.component != release.component:
            raise ValueError("component_release_binding")
        if self.deployment.source_identity(release.component) != operation.expected_source:
            raise ValueError("component_source_changed")
        if (datetime_now() - operation.created_at).total_seconds() > 1800:
            raise ValueError("component_operation_expired")
        current = next(
            row for row in inventory(self.deployment) if row["component"] == release.component
        )
        if release.component in COMPATIBLE and not COMPATIBLE[release.component](
            current["version"], release.native_version
        ):
            raise ValueError("component_data_migration_required")
        directory = self.directory(identity)
        directory.mkdir(mode=0o700)
        path = directory / "candidate.tar"
        self.client.download(release, path)
        self.verify_archive(path, release)
        snapshot = {
            "phase": "prepared",
            "selectors": self.deployment.selectors(),
            "images": {
                name: self.deployment.inspect(name)["Image"]
                for name in self.deployment.services(release.component)
            },
            "schema": schema_revision(self.deployment),
            "component": release.component,
            "source": operation.expected_source,
            "native_version": current["version"],
            "utility_image": self.deployment.inspect("backend")["Image"],
            "writer": capture_writer(self.deployment) if release.component == "s3" else None,
        }
        private_json(directory / "snapshot.json", snapshot)
        if release.data_policy == "alembic" and snapshot["schema"] != release.schema_from:
            raise ValueError("platform_schema_changed")
        return {"backup_id": str(identity)}

    def verify_archive(self, path: Path, release: ComponentRelease) -> None:
        DockerArchiveVerifier(lambda *_: release.native_version).verify(path, release)
        for component in SERVICES:
            for service in self.deployment.services(component):
                row = self.deployment.inspect(service)
                if (
                    row["Config"]["Image"].split("@")[0] == release.candidate_image
                    and row["Image"] != release.expected_image_id
                ):
                    raise ValueError("component_active_image_tag_conflict")
        self.deployment.runner.run(["docker", "load", "-i", str(path)], 600)
        image = json.loads(
            self.deployment.runner.run(["docker", "image", "inspect", release.candidate_image])
        )[0]
        if (
            # Linux keeps the remote engine ID, cryptographically bound by the
            # archive; this Windows/containerd verifier may use an OCI index ID.
            release.component != "linux-terminal" and image["Id"] != release.expected_image_id
        ) or image["Architecture"] != release.architecture:
            raise ValueError("component_loaded_image_mismatch")
        if release.component == "s3":
            layout = storage_layout(self.deployment.config())
            validate_s3_image(layout, image)
            if layout == "bundle":
                probe_storage_layout(self.deployment, image["Id"])
        if probe_image(self.deployment, release.component, image["Id"]) != release.native_version:
            raise ValueError("component_native_version_mismatch")
        if (
            release.maa_version is not None
            and probe_image(self.deployment, "maa", image["Id"]) != release.maa_version
        ):
            raise ValueError("component_maa_version_mismatch")

    def switch(self, identity: UUID, payload: JsonObject, point: JsonObject) -> None:
        release = ComponentRelease.model_validate(payload["release"])
        directory = self.directory(identity)
        snapshot = json.loads((directory / "snapshot.json").read_text())
        if self.deployment.source_identity(release.component) != snapshot["source"]:
            raise ValueError("component_source_changed")
        snapshot["phase"] = "quiescing"
        private_json(directory / "snapshot.json", snapshot)
        writer_action(self.deployment, snapshot.get("writer"), "stop")
        self.deployment.stop(("backend",))
        if release.component != "platform":
            if release.component == "postgresql":
                backup_data(self.deployment, "postgresql", directory, snapshot["utility_image"])
            self.deployment.stop(self.deployment.services(release.component))
        if release.component in {"platform", "s3", "mqtt"}:
            backup_data(self.deployment, release.component, directory, snapshot["utility_image"])
        snapshot["phase"] = "activating"
        private_json(directory / "snapshot.json", snapshot)
        self.deployment.change_image(release.component, release.candidate_image)
        if release.component == "platform":
            platform.migrate(self.deployment, release)
        self.deployment.start(self.deployment.services(release.component))
        if release.component != "platform":
            self.deployment.start(("backend",))

    def verify(self, identity: UUID, payload: JsonObject, point: JsonObject) -> bool:
        release = ComponentRelease.model_validate(payload["release"])
        for _ in range(60):
            rows = [
                self.deployment.inspect(name)
                for name in self.deployment.services(release.component)
            ]
            healthy = all(
                row["Image"] == release.expected_image_id
                and row["State"].get("Running")
                and row["State"].get("Health", {}).get("Status", "healthy") == "healthy"
                for row in rows
            )
            backend = self.deployment.inspect("backend")
            if healthy and backend["State"].get("Health", {}).get("Status") == "healthy":
                actual = next(
                    row
                    for row in inventory(self.deployment)
                    if row["component"] == release.component
                )
                valid = bool(actual["version"] == release.native_version)
                if valid:
                    snapshot = json.loads((self.directory(identity) / "snapshot.json").read_text())
                    writer_action(self.deployment, snapshot.get("writer"), "start")
                return valid
            time.sleep(2)
        return False

    def recover(self, identity: UUID, payload: JsonObject, point: JsonObject) -> str:
        directory = self.directory(identity)
        path = directory / "snapshot.json"
        if not path.exists():
            return "unknown"
        snapshot = json.loads(path.read_text())
        release = ComponentRelease.model_validate(payload["release"])
        if snapshot.get("component") != release.component:
            return "unknown"
        if snapshot.get("phase") in {"prepared", "quiescing"}:
            # No candidate or migration has run. A stopped database must restart
            # before its schema can be read; never switch an unexpected image.
            if any(
                self.deployment.inspect(name)["Image"] != image
                for name, image in snapshot["images"].items()
            ):
                return "unknown"
            self.deployment.write_selectors(snapshot["selectors"])
            self.deployment.start(self.deployment.services(release.component))
            self.deployment.start(("backend",))
            return self.verify_original(snapshot)
        if (
            release.data_policy != "same-format"
            or schema_revision(self.deployment) != snapshot["schema"]
        ):
            return "unknown"
        backups = json.loads((directory / "backups.json").read_text())
        for name, digest in backups.items():
            item = (directory / name).resolve()
            if not item.is_relative_to(directory.resolve()) or checksum(item) != digest:
                return "unknown"
        self.deployment.stop(("backend",))
        self.deployment.stop(self.deployment.services(release.component))
        self.deployment.write_selectors(snapshot["selectors"])
        self.deployment.start(self.deployment.services(release.component))
        if release.component != "platform":
            self.deployment.start(("backend",))
        return self.verify_original(snapshot)

    def verify_original(self, snapshot: JsonObject) -> str:
        for _ in range(60):
            rows = [self.deployment.inspect(name) for name in snapshot["images"]]
            if all(
                row["Image"] == snapshot["images"][name] and row["State"].get("Running")
                for name, row in zip(snapshot["images"], rows, strict=True)
            ):
                backend = self.deployment.inspect("backend")
                if (
                    backend["State"].get("Health", {}).get("Status") == "healthy"
                    and schema_revision(self.deployment) == snapshot["schema"]
                ):
                    writer_action(self.deployment, snapshot.get("writer"), "start")
                    return "failed_safe"
            time.sleep(2)
        return "unknown"


def datetime_now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)
