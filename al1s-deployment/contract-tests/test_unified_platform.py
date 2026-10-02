import configparser
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = {"api", "events", "scheduler", "media", "notifications", "terminal-monitor", "release-verifier"}


def load_healthcheck():
    spec = importlib.util.spec_from_file_location(
        "platform_healthcheck", ROOT / "platform/healthcheck.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unified_services():
    config = yaml.safe_load((ROOT / "compose/compose.unified.yaml").read_text())
    services = config["services"]
    assert not {"frontend", "worker", "media-worker", "notification-worker"} & services.keys()
    assert services["backend"]["build"]["dockerfile"] == "al1s-deployment/platform/Dockerfile"
    assert services["backend"]["healthcheck"]["test"][-1] == "/app/healthcheck.py"
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    supervisor = configparser.ConfigParser()
    supervisor.read(ROOT / "platform/supervisord.conf")
    # Supervisord stops programs with the same priority together. Each stopwaitsecs
    # is the process's own TERM-to-KILL deadline, not an additive timeout.
    priorities = {supervisor[f"program:{name}"].getint("priority", fallback=999) for name in PROGRAMS}
    assert len(priorities) == 1
    shutdown_budget = max(int(supervisor[f"program:{name}"]["stopwaitsecs"]) for name in PROGRAMS)
    assert int(services["backend"]["stop_grace_period"][:-1]) >= shutdown_budget + 30


def test_supervised_programs():
    config = configparser.ConfigParser()
    config.read(ROOT / "platform/supervisord.conf")
    assert "inet_http_server" not in config
    assert config["unix_http_server"]["chmod"] == "0600"
    programs = [name for name in config.sections() if name.startswith("program:")]
    assert set(programs) == {f"program:{name}" for name in PROGRAMS}
    for name in programs:
        assert config[name]["stopasgroup"] == "true"
        assert config[name]["killasgroup"] == "true"
        assert config[name]["stopwaitsecs"] == "60"


def test_tls_targets_unified_backend():
    config = yaml.safe_load((ROOT / "compose/compose.tls.yaml").read_text())
    assert "worker" not in config["services"]
    assert "backend" in config["services"]["tls-gateway"]["depends_on"]
    assert "backend:8000" in (ROOT / "compose/tls/nginx.conf").read_text()


def test_smoke_has_separate_resources():
    smoke = yaml.safe_load((ROOT / "compose/compose.smoke.yaml").read_text())
    platform = yaml.safe_load((ROOT / "compose/compose.platform.yaml").read_text())
    unified = yaml.safe_load((ROOT / "compose/compose.unified.yaml").read_text())
    marker = "${AL1S_SMOKE_ID:?set a unique smoke identifier}"
    assert smoke["name"] == f"al1s-smoke-{marker}"
    assert set(smoke["services"]) == set(unified["services"])
    assert all(marker in service["container_name"] for service in smoke["services"].values())
    assert not {service.get("container_name") for service in platform["services"].values()} & {
        service["container_name"].replace(marker, "review-unique")
        for service in smoke["services"].values()
    }
    for kind in ("networks", "volumes"):
        live_names = {resource["name"] for resource in platform[kind].values()}
        smoke_names = {
            resource["name"].replace(marker, "review-unique")
            for resource in smoke[kind].values()
        }
        assert all(resource["external"] is False for resource in smoke[kind].values())
        assert all(marker in resource["name"] for resource in smoke[kind].values())
        assert not live_names & smoke_names


def test_effective_smoke_compose_cannot_reuse_platform_identities(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("AL1S_")
    }
    environment.update(
        {
            "AL1S_BOT_CONTROL_TOKEN": "contract-bot-token-" + "0" * 13,
            "AL1S_DOCKER_SOCKET_GID": "0",
            "AL1S_SMOKE_ID": "contract-unique",
            "AL1S_PLATFORM_IMAGE": "al1s-platform:contract-placeholder",
            "AL1S_OFFLINE_PERMIT_SIGNING_KEY": "contract-offline-permit-key-" + "0" * 16,
        }
    )
    env_file = tmp_path / "compose-empty.env"
    env_file.write_text("", encoding="utf-8")
    common = ["docker", "compose", "--env-file", str(env_file)]

    def config(*files: str) -> dict:
        command = common + [item for file in files for item in ("-f", file)]
        result = subprocess.run(
            command + ["config", "--format", "json"], cwd=ROOT,
            env=environment, capture_output=True, text=True, timeout=15, check=True,
        )
        return json.loads(result.stdout)

    live = config("compose/compose.unified.yaml", "compose/compose.tls.yaml",
                  "compose/compose.platform.yaml")
    smoke = config("compose/compose.unified.yaml", "compose/compose.smoke.yaml")

    def identities(project: dict) -> set[str]:
        names = {project["name"]}
        for kind in ("networks", "volumes"):
            names.update(value["name"] for value in project[kind].values())
        names.update(value["container_name"] for value in project["services"].values()
                     if value.get("container_name"))
        return names

    assert not identities(live) & identities(smoke)


def test_local_storage_requires_explicit_existing_directory():
    config = yaml.safe_load((ROOT / "compose/compose.storage-local.yaml").read_text())
    assert set(config["services"]) == {"seaweed-master", "seaweed-volume", "seaweed-filer"}
    for service in config["services"].values():
        mount = service["volumes"][0]
        assert mount["type"] == "bind"
        assert mount["target"] == "/data"
        assert "AL1S_STORAGE_DIRECTORY:?" in mount["source"]
        assert mount["bind"]["create_host_path"] is False


@pytest.mark.parametrize("bad", ["missing", "FATAL", "STOPPED", "BACKOFF"])
@pytest.mark.parametrize("program", sorted(PROGRAMS))
def test_health_requires_all_workers(bad, program):
    module = load_healthcheck()
    lines = [f"{name} RUNNING" for name in PROGRAMS if name != program]
    if bad != "missing":
        lines.append(program + " " + bad)
    result = subprocess.CompletedProcess([], 0, stdout="\n".join(lines))
    with (
        patch.object(module.subprocess, "run", return_value=result),
        pytest.raises(SystemExit),
    ):
        module.main()


def test_health_checks_api_after_processes():
    module = load_healthcheck()
    result = subprocess.CompletedProcess(
        [], 0, stdout="\n".join(f"{name} RUNNING" for name in PROGRAMS)
    )
    with (
        patch.object(module.subprocess, "run", return_value=result),
        patch.object(module.urllib.request, "urlopen", return_value=MagicMock()) as request,
    ):
        module.main()
        request.assert_called_once()


def test_health_does_not_hide_api_failure():
    module = load_healthcheck()
    result = subprocess.CompletedProcess(
        [], 0, stdout="\n".join(f"{name} RUNNING" for name in PROGRAMS)
    )
    with (
        patch.object(module.subprocess, "run", return_value=result),
        patch.object(module.urllib.request, "urlopen", side_effect=TimeoutError),
        pytest.raises(TimeoutError),
    ):
        module.main()
