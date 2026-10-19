import json
from unittest.mock import MagicMock

import pytest

from al1s.components.contracts import ComponentRelease
from al1s.components.host.bot_driver import BotUpgradeDriver, protected_state
from al1s.components.host.deployment_router import DeploymentRouter
from tests.test_bot_upgrade_driver import upgrade_payload


def fixture(tmp_path, running=True):
    identity, payload = upgrade_payload()
    state = {
        "Image": "sha256:" + "b" * 64,
        "Id": "target",
        "State": {"Running": running, "Health": {"Status": "healthy"}},
        "Config": {"Env": [], "User": "root", "Image": "al1s-plachta:qq-old"},
        "HostConfig": {"PortBindings": {}},
        "Mounts": [],
        "NetworkSettings": {"Networks": {}},
    }
    snapshot = {
        "alias": "qq",
        "peer": "discord",
        "peer_id": "old-peer",
        "peer_started": "old-start",
        "candidate_id": state["Image"],
        "before_id": "sha256:" + "a" * 64,
        "before_image": "al1s-plachta:qq-old",
        "running": running,
        "data_format": "same-format",
        "protected": protected_state(state),
        "before_proof": {
            "role": "qq",
            "native_version": "8.1.9",
            "runtime_version": "1.0.0",
            "render_proof": True,
        },
        "operational_identity": {
            "role": "qq",
            "binding": {"account": "10001", "device_guid_sha256": "a" * 64, "protocol": "linux"},
            "account_state": "online",
        },
        "selectors": {
            "AL1S_QQ_BOT_IMAGE": "al1s-plachta:qq-old",
            "AL1S_DISCORD_WORKER_IMAGE": "al1s-plachta:dd-old",
        },
    }
    deployment = MagicMock()
    from al1s.components.host.bot_deployment import BotDeployment

    deployment.reject_candidate_tag_collision = lambda image, image_id: (
        BotDeployment.reject_candidate_tag_collision(deployment, image, image_id)
    )
    deployment.inspect.side_effect = lambda alias: (
        state if alias == "qq" else {"Id": "new-peer", "State": {"StartedAt": "new-start"}}
    )
    deployment.runner.run.side_effect = lambda args, *_rest: json.dumps(
        snapshot["operational_identity"]
        if args[-1] == "al1s.meme_workshop.operation_identity"
        else snapshot["before_proof"]
    )
    driver = BotUpgradeDriver(deployment, MagicMock(), tmp_path)
    driver.directory(identity).mkdir(parents=True)
    (driver.directory(identity) / "snapshot.json").write_text(json.dumps(snapshot))
    return driver, deployment, identity, payload, state, snapshot


def test_peer_maintenance_does_not_invalidate_proven_target(tmp_path):
    driver, _, identity, payload, _, _ = fixture(tmp_path)
    assert driver.verify(identity, payload, {})
    assert driver.recover(identity, payload, {}) == "succeeded"


@pytest.mark.parametrize("mode,expected", [("default", True), ("override", False),
                                           ("spoof", False), ("binary", False)])
def test_official_node_default_change_preserves_only_verified_image_metadata(tmp_path, mode, expected):
    driver, deployment, identity, _, state, snapshot = fixture(tmp_path)
    snapshot["protected"]["env"] = sorted(["TOKEN=private-test", "NODE_VERSION=24.9.0"])
    state["Config"]["Env"] = ["TOKEN=private-test", "NODE_VERSION=24.21.0"]
    deployment.config.return_value = {"services": {"llbot": {"environment":
        {"NODE_VERSION": "24.9.0"} if mode == "override" else {}}}}
    original = deployment.runner.run.side_effect

    def run(args, *rest):
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([
                {"Id": snapshot["before_id"], "Config": {"Env": ["NODE_VERSION=24.9.0"]}},
                {"Id": state["Image"], "Config": {"Env": ["NODE_VERSION=" +
                    ("99.0.0" if mode == "spoof" else "24.21.0")]}},
            ])
        if args[-2:] == ["node", "--version"]:
            return "v99.0.0" if mode == "binary" else "v24.21.0"
        return original(args, *rest)

    deployment.runner.run.side_effect = run
    (driver.directory(identity) / "snapshot.json").write_text(json.dumps(snapshot))
    assert driver.protected_matches(state, snapshot) is expected


@pytest.mark.parametrize(
    "source,expected",
    [
        ("/run/desktop/mnt/host/c/Users/YY/AL1S/al1s-deployment/bots/llbot-data", True),
        ("/run/desktop/mnt/host/c/Users/YY/AL1S/al1s-deployment/bots/other-data", False),
        ("/run/desktop/mnt/host/c/Users/YY/AL1S/al1s-deployment/../other-data", False),
    ],
)
def test_desktop_bind_alias_is_accepted_only_for_the_same_registered_directory(
    tmp_path, source, expected
):
    from pathlib import Path

    driver, deployment, identity, payload, state, snapshot = fixture(tmp_path)
    deployment.settings.deployment_root = Path(
        "/run/desktop/mnt/host/c/Users/YY/AL1S/al1s-deployment"
    )
    mount = {
        "Type": "bind",
        "Source": r"C:\Users\YY\AL1S\al1s-deployment\bots\llbot-data",
        "Destination": "/app/llbot/data",
        "Mode": "rw",
        "RW": True,
        "Propagation": "rprivate",
    }
    snapshot["protected"]["mounts"] = [mount]
    (driver.directory(identity) / "snapshot.json").write_text(json.dumps(snapshot))
    state["Mounts"] = [{**mount, "Source": source}]
    assert driver.verify(identity, payload, {}) is expected
    assert driver.recover(identity, payload, {}) == ("succeeded" if expected else "unknown")


def test_recovery_repairs_selector_before_unlocking_unchanged_stopped_source(tmp_path):
    driver, deployment, identity, payload, state, snapshot = fixture(tmp_path, running=False)
    from al1s.meme_workshop.operation_identity import OperationIdentity

    driver.archived_identity = lambda *_: OperationIdentity.model_validate(
        snapshot["operational_identity"]
    )
    deployment.inspect.side_effect = lambda alias: (
        state
        if alias == "qq"
        else {"Id": snapshot["peer_id"], "State": {"StartedAt": snapshot["peer_started"]}}
    )
    state["Image"] = snapshot["before_id"]
    deployment.selectors.return_value = {
        **snapshot["selectors"],
        "AL1S_QQ_BOT_IMAGE": "al1s-plachta:qq-new",
    }
    deployment.runner.run.return_value = json.dumps([{"Id": snapshot["before_id"]}])
    deployment.runner.run.side_effect = None
    deployment.change_image.side_effect = lambda alias, image: (
        deployment.selectors.return_value.update(AL1S_QQ_BOT_IMAGE=image)
    )
    assert driver.recover(identity, payload, {}) == "failed_safe"
    deployment.change_image.assert_called_once_with("qq", "al1s-plachta:qq-old")
    assert deployment.selectors()["AL1S_QQ_BOT_IMAGE"] == "al1s-plachta:qq-old"


def test_stopped_identity_is_read_again_from_current_files(tmp_path):
    driver, deployment, identity, _, state, _ = fixture(tmp_path, running=False)
    state["Config"]["Env"] = ["AUTO_LOGIN_QQ=10001"]
    import tarfile
    from io import BytesIO

    values = {"machine_guid.bin": b"a" * 16, "config_10001.json": b"{}"}

    def stream(args, destination, *_):
        name = args[2].rsplit("/", 1)[1]
        with tarfile.open(destination, "w") as output:
            member = tarfile.TarInfo(name)
            member.size = len(values[name])
            output.addfile(member, BytesIO(values[name]))

    deployment.runner.stream.side_effect = stream
    first = driver.archived_identity("qq", identity, state)
    values["machine_guid.bin"] = b"b" * 16
    second = driver.archived_identity("qq", identity, state)
    assert first.binding != second.binding


def test_archive_publication_cannot_replace_a_bot_selector_tag(tmp_path, monkeypatch):
    _, payload = upgrade_payload()
    release = ComponentRelease.model_validate(payload["release"])
    deployment = MagicMock()
    from al1s.components.host.bot_deployment import BotDeployment

    deployment.reject_candidate_tag_collision = lambda image, image_id: (
        BotDeployment.reject_candidate_tag_collision(deployment, image, image_id)
    )
    deployment.inspect_all.return_value = {
        "qq": {"Image": "sha256:" + "a" * 64, "Config": {"Image": release.candidate_image}}
    }
    deployment.selectors.return_value = {
        "AL1S_QQ_BOT_IMAGE": release.candidate_image,
        "AL1S_DISCORD_WORKER_IMAGE": "al1s-plachta:dd-old",
    }
    deployment.runner.run.return_value = json.dumps(
        [
            {
                "Id": release.expected_image_id,
                "Architecture": "amd64",
                "Config": {
                    "Labels": {"org.al1s.bot-role": "qq", "org.al1s.native-version": "8.1.9"}
                },
            }
        ]
    )
    from al1s.components.host.bot_proof import RuntimeProof

    monkeypatch.setattr(
        "al1s.components.host.deployment_router.probe_bot_image",
        lambda *_: RuntimeProof(
            role="qq", native_version="8.1.9", runtime_version="1.0.0", render_proof=True
        ),
    )
    monkeypatch.setattr(
        "al1s.components.host.deployment_router.DockerArchiveVerifier.verify", lambda *_: None
    )
    router = DeploymentRouter(MagicMock(), BotUpgradeDriver(deployment, MagicMock(), tmp_path))
    with pytest.raises(ValueError, match="tag_conflict"):
        router.verify_archive(tmp_path / "candidate.tar", release)
    assert not any(
        call.args[0][:2] == ["docker", "load"] for call in deployment.runner.run.call_args_list
    )
