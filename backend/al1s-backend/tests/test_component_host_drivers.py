import copy
import json

import pytest

from al1s.components.host.config import HostSettings
from al1s.components.host.compose_driver import ComposeDeployment, SELECTORS
from al1s.components.host.probes import parsed_version


def settings(tmp_path):
    file = tmp_path / "components.env"
    file.write_text("".join(f"{key}=old:{component}\n" for component, key in SELECTORS.items()))
    return HostSettings(
        deployment_root=tmp_path,
        state_dir=tmp_path / "state",
        compose_files=[tmp_path / "compose.yaml"],
        env_files=[file],
        selectors_file=file,
        platform={"url": "https://platform.local", "token": "x" * 32},
        certificate_file=tmp_path / "server.crt",
        private_key_file=tmp_path / "server.key",
        allowed_origin="https://platform.local",
    )


def test_image_selection_preserves_complete_configuration_and_rejects_other_changes(tmp_path):
    config = settings(tmp_path)
    original = {
        "name": "al1s-platform",
        "networks": {"control": {"external": True}},
        "volumes": {"database": {"external": True}},
        "services": {
            name: {
                "image": image,
                "container_name": "al1s-platform-" + name,
                "environment": {"SECRET": "private"},
            }
            for name, image in [
                ("backend", "old:platform"),
                ("postgres", "old:postgresql"),
                ("mosquitto", "old:mqtt"),
            ]
        },
    }

    class Runner:
        sabotage = False

        def run(self, arguments, timeout=30):
            current = copy.deepcopy(original)
            values = dict(
                line.split("=", 1) for line in config.selectors_file.read_text().splitlines()
            )
            current["services"]["postgres"]["image"] = values[SELECTORS["postgresql"]]
            if self.sabotage:
                current["services"]["backend"]["environment"]["SECRET"] = "changed"
            return json.dumps(current)

    runner = Runner()
    deployment = ComposeDeployment(config, runner)
    deployment.change_image("postgresql", "postgres:17.12")
    result = deployment.config()
    expected = copy.deepcopy(original)
    expected["services"]["postgres"]["image"] = "postgres:17.12"
    assert result == expected
    runner.sabotage = True
    # A concurrent change existing before the snapshot is part of the snapshot; inject after write.
    original_write = deployment.write_selectors

    def sabotage(values):
        original_write(values)
        runner.sabotage = not runner.sabotage

    deployment.write_selectors = sabotage
    before = deployment.selectors()
    with pytest.raises(ValueError):
        deployment.change_image("postgresql", "postgres:17.13")
    assert deployment.selectors() == before


def test_native_version_parsers_are_service_specific():
    assert parsed_version("postgresql", "postgres (PostgreSQL) 17.11") == "17.11"
    assert parsed_version("s3", "version 30GB 4.43 abc linux amd64") == "4.43"
    assert parsed_version("mqtt", "mosquitto version 2.1.2") == "2.1.2"
    with pytest.raises(ValueError):
        parsed_version("postgresql", "private-token")


def test_quiescing_recovery_restarts_original_before_reading_database(tmp_path, monkeypatch):
    from uuid import uuid4
    from al1s.components.host.deployment_driver import DeploymentDriver
    from tests.test_component_inventory import release
    identity = uuid4()
    calls = []
    class Deployment:
        settings = type('Settings', (), {'state_dir': tmp_path})()
        def services(self, component):
            from al1s.components.host.layout import SERVICES
            return SERVICES[component]
        def start(self, services): calls.append(('start', services))
        def stop(self, services): calls.append(('stop', services))
        def write_selectors(self, values): calls.append(('selectors', values))
        def inspect(self, name): return {'Image': 'old', 'State': {'Running': any(item[0] == 'start' for item in calls), 'Health': {'Status': 'healthy'}}}
    driver = DeploymentDriver(Deployment(), None)
    directory = driver.directory(identity); directory.mkdir()
    (directory / 'snapshot.json').write_text(json.dumps({'phase': 'quiescing', 'component': 'postgresql', 'schema': '0052', 'selectors': {}, 'images': {'postgres': 'old'}}))
    def revision(_deployment):
        assert any(item[0] == 'start' for item in calls), 'database is stopped'
        return '0052'
    monkeypatch.setattr('al1s.components.host.deployment_driver.schema_revision', revision)
    body = {**release().model_dump(mode='json'), 'state': 'published', 'row_version': 1, 'created_at': '2026-10-04T00:00:00Z'}
    result = driver.recover(identity, {'release': body}, {})
    assert result == 'failed_safe'


def test_linux_archive_keeps_remote_identity_but_probes_loaded_local_image(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4
    from al1s.components.contracts import ComponentRelease
    from al1s.components.host.deployment_driver import DeploymentDriver
    from tests.test_component_inventory import release
    from tests.test_component_releases import archive
    path=tmp_path/'linux.tar'
    expected=archive(path,architecture='arm64',tag='al1s-terminal:linux-cross-engine')
    body=ComponentRelease(**release(component='linux-terminal',architecture='arm64',candidate_image='al1s-terminal:linux-cross-engine',expected_image_id=expected,size_bytes=path.stat().st_size,native_version='0.1.0',compatible_from='0.1',maa_version='v5.12.1').model_dump(),state='published',row_version=1,created_at=datetime.now(UTC),blob_id=uuid4())
    actual='sha256:'+'c'*64
    class Runner:
        def run(self,args,timeout=30):return json.dumps([{'Id':actual,'Architecture':'arm64'}]) if args[:3]==['docker','image','inspect'] else ''
    class Deployment:
        runner=Runner()
        settings=type('Settings',(),{'state_dir':tmp_path})()
        def services(self, component):
            from al1s.components.host.layout import SERVICES
            return SERVICES[component]
        def inspect(self,_):return {'Config':{'Image':'original:version'},'Image':'sha256:'+'d'*64}
    calls=[]
    def probe(_deployment,component,image):
        calls.append((component,image));return 'v5.12.1' if component=='maa' else '0.1.0'
    monkeypatch.setattr('al1s.components.host.deployment_driver.probe_image',probe)
    DeploymentDriver(Deployment(),None).verify_archive(path,body)
    assert calls==[('linux-terminal',actual),('maa',actual)]


def test_stream_gzip_round_trip_and_failed_producer(tmp_path):
    import gzip
    import subprocess
    import sys

    from al1s.components.host.commands import CommandRunner

    runner = CommandRunner()
    archive = tmp_path / "stream.gz"
    payload = b"backup-data-" * 100_000
    runner.stream_gzip(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'backup-data-'*100000)"],
        archive,
        30,
    )
    with gzip.open(archive, "rb") as source:
        assert source.read() == payload

    failed = tmp_path / "failed.gz"
    with pytest.raises(subprocess.CalledProcessError):
        runner.stream_gzip(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'partial');sys.exit(7)"],
            failed,
            30,
        )
    assert not failed.exists()
    existing = tmp_path / "existing.gz"
    existing.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        runner.stream_gzip([sys.executable, "-c", "pass"], existing, 30)
    assert existing.read_bytes() == b"existing"


def test_stream_gzip_timeout_removes_partial_file(tmp_path):
    import subprocess
    import sys

    from al1s.components.host.commands import CommandRunner

    destination = tmp_path / "timeout.gz"
    with pytest.raises(subprocess.TimeoutExpired):
        CommandRunner().stream_gzip(
            [sys.executable, "-c", "import time;time.sleep(10)"], destination, 0.5
        )
    assert not destination.exists()


def test_stream_gzip_failed_compressor_removes_partial_file(tmp_path, monkeypatch):
    import subprocess
    import sys

    from al1s.components.host import commands

    start = subprocess.Popen

    def launch(arguments, *args, **kwargs):
        if len(arguments) >= 3 and "gzip.GzipFile" in arguments[2]:
            arguments = [sys.executable, "-c", "import sys;sys.exit(23)"]
        return start(arguments, *args, **kwargs)

    monkeypatch.setattr(commands.subprocess, "Popen", launch)
    destination = tmp_path / "compressor-failed.gz"
    with pytest.raises(subprocess.CalledProcessError):
        commands.CommandRunner().stream_gzip(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*1000000)"],
            destination,
            30,
        )
    assert not destination.exists()


@pytest.mark.parametrize("component", ["s3", "mqtt"])
def test_component_directory_backup_is_compressed_and_verified(tmp_path, component):
    import hashlib
    import subprocess
    import sys
    import tarfile

    from al1s.components.host.backup import backup_data

    source = tmp_path / "source"
    source.mkdir()
    payload = b"volume-data-" * 100_000
    (source / "payload.bin").write_bytes(payload)
    directory = tmp_path / "recovery"
    directory.mkdir()

    class Runner:
        corrupt = False

        def run(self, arguments, timeout=30):
            archive = directory / "data-0" / "data.tar.gz"
            program = arguments[-1].replace("'/source'", repr(str(source)))
            program = program.replace("'/backup/data.tar.gz'", repr(str(archive)))
            subprocess.run([sys.executable, "-c", program], check=True, timeout=timeout)
            if self.corrupt:
                archive.write_bytes(archive.read_bytes()[:-8])
            return ""

    class Deployment:
        runner = Runner()

        def services(self, requested):
            assert requested == component
            return ["storage" if component == "s3" else "mosquitto"]

        def inspect(self, service):
            assert service == ("storage" if component == "s3" else "mosquitto")
            return {"Mounts": [{"RW": True, "Type": "bind", "Source": str(source)}]}

    deployment = Deployment()
    result = backup_data(deployment, component, directory, "utility:image")
    archive = directory / "data-0" / "data.tar.gz"
    assert result == {"data-0/data.tar.gz": hashlib.sha256(archive.read_bytes()).hexdigest()}
    assert json.loads((directory / "backups.json").read_text()) == result
    with tarfile.open(archive, "r:gz") as packed:
        assert packed.extractfile("data/payload.bin").read() == payload
    directory = tmp_path / "failed-recovery"
    directory.mkdir()
    deployment.runner.corrupt = True
    with pytest.raises((EOFError, OSError, tarfile.TarError)):
        backup_data(deployment, component, directory, "utility:image")
    assert not (directory / "backups.json").exists()
    assert not (directory / "data-0" / "data.tar.gz").exists()


def test_postgresql_backup_keeps_native_archive_format(tmp_path):
    import hashlib

    from al1s.components.host.backup import backup_data

    directory = tmp_path / "recovery"
    directory.mkdir()

    class Runner:
        def stream(self, arguments, destination, timeout):
            assert "pg_dump" in arguments[-1] and "-Fc" in arguments[-1]
            destination.write_bytes(b"native-postgresql-archive")

        def run(self, arguments, timeout=30):
            assert "pg_restore" in arguments and "/backup/database.dump" in arguments
            return ""

    class Deployment:
        runner = Runner()

        def inspect(self, service):
            assert service == "postgres"
            return {"Id": "postgres-id", "Image": "postgres-image"}

    result = backup_data(Deployment(), "postgresql", directory, "utility:image")
    archive = directory / "database.dump"
    assert result == {"database.dump": hashlib.sha256(archive.read_bytes()).hexdigest()}
    assert not list(directory.glob("*.gz"))


@pytest.mark.parametrize("damage", ["truncated", "checksum"])
def test_gzip_archive_validation_rejects_corruption(tmp_path, damage):
    import tarfile

    from al1s.components.host.backup import validate_gzip_archive

    source = tmp_path / "payload"
    source.write_bytes(b"important data" * 10_000)
    archive = tmp_path / "data.tar.gz"
    with tarfile.open(archive, "w:gz") as packed:
        packed.add(source, arcname="payload")
    validate_gzip_archive(archive)
    data = archive.read_bytes()
    data = data[:-8] if damage == "truncated" else data[:-8] + bytes([data[-8] ^ 1]) + data[-7:]
    archive.write_bytes(data)
    with pytest.raises((EOFError, OSError, tarfile.TarError)):
        validate_gzip_archive(archive)


@pytest.mark.parametrize("name", ["data-0/data.tar", "data-0/data.tar.gz"])
def test_component_recovery_accepts_old_and_new_backup_manifest(tmp_path, monkeypatch, name):
    import hashlib
    import tarfile
    from uuid import uuid4

    from al1s.components.host.deployment_driver import DeploymentDriver
    from tests.test_component_inventory import release

    class Deployment:
        settings = type("Settings", (), {"state_dir": tmp_path})()

        def __init__(self):
            self.stopped = []

        def services(self, component):
            return [component]

        def stop(self, services):
            self.stopped.append(tuple(services))

        def start(self, services):
            pass

        def write_selectors(self, selectors):
            pass

    deployment = Deployment()
    driver = DeploymentDriver(deployment, None)
    identity = uuid4()
    directory = driver.directory(identity)
    directory.mkdir()
    archive = directory / name
    archive.parent.mkdir()
    source = tmp_path / "protected-backup"
    source.write_bytes(b"protected-backup")
    with tarfile.open(archive, "w:gz" if name.endswith(".gz") else "w") as packed:
        packed.add(source, arcname="protected-backup")
    (directory / "snapshot.json").write_text(json.dumps({
        "phase": "activating", "component": "postgresql",
        "schema": "0052", "selectors": {},
    }))
    (directory / "backups.json").write_text(json.dumps({
        name: hashlib.sha256(archive.read_bytes()).hexdigest(),
    }))
    monkeypatch.setattr("al1s.components.host.deployment_driver.schema_revision", lambda _: "0052")
    monkeypatch.setattr(driver, "verify_original", lambda _: "failed_safe")
    body = {**release().model_dump(mode="json"), "state": "published",
            "row_version": 1, "created_at": "2026-10-04T00:00:00Z"}
    assert driver.recover(identity, {"release": body}, {}) == "failed_safe"

    deployment.stopped.clear()
    archive.write_bytes(b"corrupted-backup")
    assert driver.recover(identity, {"release": body}, {}) == "unknown"
    assert not deployment.stopped
    if name.endswith(".gz"):
        (directory / "backups.json").write_text(json.dumps({
            name: hashlib.sha256(archive.read_bytes()).hexdigest(),
        }))
        assert driver.recover(identity, {"release": body}, {}) == "unknown"
        assert not deployment.stopped
