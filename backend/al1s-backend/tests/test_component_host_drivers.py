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
