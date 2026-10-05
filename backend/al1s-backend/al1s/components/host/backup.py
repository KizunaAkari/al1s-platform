import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from al1s.components.host.compose_driver import SERVICES, ComposeDeployment
from al1s.components.host.contracts import JsonObject

ARCHIVE_PROGRAM = (
    "import tarfile; t=tarfile.open('/backup/data.tar','w');"
    "t.add('/source',arcname='data');t.close()"
)


def private_json(path: Path, value: JsonObject) -> None:
    temporary = path.with_name("." + str(uuid4()) + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def schema_revision(deployment: ComposeDeployment) -> str:
    postgres = deployment.inspect("postgres")
    script = (
        'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -U "$POSTGRES_USER" '
        '-d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"'
    )
    return deployment.runner.run(["docker", "exec", postgres["Id"], "sh", "-ec", script], 30)


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while body := stream.read(1024**2):
            digest.update(body)
    return digest.hexdigest()


def backup_data(
    deployment: ComposeDeployment, component: str, directory: Path, utility_image: str
) -> JsonObject:
    result = {}
    if component in {"platform", "postgresql"}:
        destination = directory / "database.dump"
        postgres = deployment.inspect("postgres")
        script = (
            'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump -U "$POSTGRES_USER" '
            '-d "$POSTGRES_DB" -Fc'
        )
        deployment.runner.stream(
            ["docker", "exec", postgres["Id"], "sh", "-ec", script], destination, 600
        )
        deployment.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--mount",
                f"type=bind,source={directory},target=/backup,readonly",
                "--entrypoint",
                "pg_restore",
                postgres["Image"],
                "--list",
                "/backup/database.dump",
            ],
            60,
        )
        result["database.dump"] = checksum(destination)
    if component in {"s3", "mqtt"}:
        sources = {}
        for service in SERVICES[component]:
            for mount in deployment.inspect(service)["Mounts"]:
                if mount.get("RW") and mount["Type"] in {"volume", "bind"}:
                    source = mount.get("Name") if mount["Type"] == "volume" else mount["Source"]
                    sources[(mount["Type"], source)] = True
        for index, (kind, source) in enumerate(sources):
            target = directory / f"data-{index}"
            target.mkdir(mode=0o700)
            deployment.runner.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "0:0",
                    "--mount",
                    f"type={kind},source={source},target=/source,readonly",
                    "--mount",
                    f"type=bind,source={target},target=/backup",
                    "--entrypoint",
                    "python",
                    utility_image,
                    "-c",
                    ARCHIVE_PROGRAM,
                ],
                600,
            )
            archive = target / "data.tar"
            result[f"data-{index}/data.tar"] = checksum(archive)
    private_json(directory / "backups.json", result)
    return result
