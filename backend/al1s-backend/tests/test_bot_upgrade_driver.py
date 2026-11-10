import json
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.components.contracts import ComponentOperation, ComponentRelease, ComponentTarget
from al1s.components.host.bot_deployment import BotDeployment, BotDeploymentSettings
from al1s.components.host.bot_driver import BotUpgradeDriver


def test_registered_bot_compose_only_recreates_selected_service(tmp_path):
    config = tmp_path / "compose.yaml"
    config.write_text("services: {}")
    env = tmp_path / "bot.env"
    env.write_text("")
    selectors = tmp_path / "images.env"
    selectors.write_text(
        "AL1S_QQ_BOT_IMAGE=al1s-plachta:qq-old\nAL1S_DISCORD_WORKER_IMAGE=al1s-plachta:dd-old\n"
    )
    settings = BotDeploymentSettings(
        deployment_root=tmp_path,
        selectors_file=selectors,
        registrations={
            a: {"compose_files": [config], "env_files": [env]} for a in ("qq", "discord")
        },
    )
    runner = MagicMock()
    deployment = BotDeployment(settings, runner)
    deployment.start("qq")
    args = runner.run.call_args.args[0]
    assert args[-1] == "llbot" and "--no-deps" in args and "-p" in args
    assert "discord-worker" not in args and str(selectors) in args


def test_selector_preserves_and_validates_headed_mode(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}")
    env = tmp_path / "bot.env"
    env.write_text("")
    selectors = tmp_path / "images.env"
    selectors.write_text("AL1S_QQ_BOT_IMAGE=al1s-plachta:qq-old\n"
                         "AL1S_DISCORD_WORKER_IMAGE=al1s-plachta:dd-old\n"
                         "AL1S_QQ_PROTOCOL_MODE=pmhq\n")
    settings = BotDeploymentSettings(deployment_root=tmp_path, selectors_file=selectors,
        registrations={a: {"compose_files": [compose], "env_files": [env]}
                       for a in ("qq", "discord")})
    deployment = BotDeployment(settings, MagicMock())
    values = deployment.selectors()
    assert values["AL1S_QQ_PROTOCOL_MODE"] == "pmhq"
    deployment.write_selectors({**values, "AL1S_QQ_BOT_IMAGE": "al1s-plachta:qq-new"})
    assert deployment.selectors()["AL1S_QQ_PROTOCOL_MODE"] == "pmhq"
    selectors.write_text(selectors.read_text() + "AL1S_QQ_PROTOCOL_MODE=direct\n")
    with pytest.raises(ValueError, match="invalid_bot_selector"):
        deployment.selectors()


def upgrade_payload():
    now, identity, release_id = datetime.now(UTC), uuid4(), uuid4()
    release = ComponentRelease(
        release_id=release_id,
        component="qq-bot",
        version="8.1.9",
        architecture="amd64",
        size_bytes=100,
        sha256="a" * 64,
        candidate_image="al1s-plachta:qq-new",
        expected_image_id="sha256:" + "b" * 64,
        native_version="8.1.9",
        compatible_from="8",
        state="published",
        blob_id=uuid4(),
        row_version=1,
        created_at=now,
        published_at=now,
    )
    operation = ComponentOperation(
        operation_id=identity,
        component="qq-bot",
        target=ComponentTarget(kind="bot", bot_alias="qq"),
        release_id=release_id,
        expected_source="c" * 64,
        state="verifying",
        stage="verify",
        row_version=1,
        created_at=now,
        updated_at=now,
    )
    return identity, {
        "operation": operation.model_dump(mode="json"),
        "release": release.model_dump(mode="json"),
    }


def test_recovery_does_not_accept_wrong_target_version(tmp_path):
    from tests.test_bot_recovery_boundaries import fixture

    driver, deployment, identity, payload, _, _ = fixture(tmp_path)
    original = deployment.runner.run.side_effect
    deployment.runner.run.side_effect = lambda args, *rest: (
        json.dumps(
            {
                "role": "qq",
                "native_version": "9.0.0",
                "runtime_version": "1.0.0",
                "render_proof": True,
            }
        )
        if args[-1] == "al1s.meme_workshop.runtime_probe"
        else original(args, *rest)
    )
    assert driver.recover(identity, payload, {}) == "unknown"
    deployment.stop.assert_not_called()


@pytest.mark.parametrize("alias", ["qq", "discord"])
@pytest.mark.parametrize("legacy", [False, True])
def test_bot_switch_uses_compressed_backup_and_preserves_legacy_resume(tmp_path, legacy, alias):
    import tarfile

    from tests.test_bot_recovery_boundaries import fixture

    driver, deployment, identity, payload, state, snapshot = fixture(tmp_path)
    if alias == "discord":
        payload["operation"]["component"] = "discord-bot"
        payload["operation"]["target"]["bot_alias"] = "discord"
        payload["release"]["component"] = "discord-bot"
        snapshot["alias"] = "discord"
        (driver.directory(identity) / "snapshot.json").write_text(json.dumps(snapshot))
        deployment.inspect.side_effect = lambda target: (
            state if target == "discord" else {"Id": "peer", "State": {"StartedAt": "start"}}
        )
    source = tmp_path / "payload"
    source.write_bytes(b"bot-data" * 1_000)
    directory = driver.directory(identity)
    raw = directory / "data.tar"
    if legacy:
        with tarfile.open(raw, "w") as archive:
            archive.add(source, arcname="payload")

    def compress(_arguments, destination, _timeout):
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(source, arcname="payload")

    deployment.source_identity.return_value = payload["operation"]["expected_source"]
    deployment.stop.side_effect = lambda _: state["State"].update(Running=False)
    deployment.runner.stream_gzip.side_effect = compress
    driver.switch(identity, payload, {})

    archive = raw if legacy else directory / "data.tar.gz"
    assert archive.exists()
    with tarfile.open(archive, "r:*") as packed:
        assert packed.extractfile("payload").read() == source.read_bytes()
    snapshot = json.loads((directory / "snapshot.json").read_text())
    assert snapshot["phase"] == "backed_up"
    assert snapshot["data_backup_bytes"] == archive.stat().st_size
    if legacy:
        deployment.runner.stream_gzip.assert_not_called()
    else:
        deployment.runner.stream_gzip.assert_called_once()


def test_bot_switch_rejects_invalid_compressed_backup_before_image_change(tmp_path):
    from tests.test_bot_recovery_boundaries import fixture

    driver, deployment, identity, payload, state, _ = fixture(tmp_path)
    deployment.source_identity.return_value = payload["operation"]["expected_source"]
    deployment.stop.side_effect = lambda _: state["State"].update(Running=False)
    deployment.runner.stream_gzip.side_effect = (
        lambda _arguments, destination, _timeout: destination.write_bytes(b"invalid gzip")
    )
    with pytest.raises((OSError, EOFError)):
        driver.switch(identity, payload, {})
    deployment.change_image.assert_not_called()
    assert not any(p.name.endswith(".gz") for p in driver.directory(identity).iterdir())


def test_bot_streamed_tar_gzip_round_trip(tmp_path):
    import hashlib
    import sys
    import tarfile

    from al1s.components.host.bot_driver import backup_identity
    from al1s.components.host.commands import CommandRunner

    source = tmp_path / "source"
    source.mkdir()
    payload = b"qq-backup-data-" * 100_000
    (source / "account.db").write_bytes(payload)
    archive = tmp_path / "data.tar.gz"
    program = (
        "import sys,tarfile;"
        "t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|');"
        "t.add(sys.argv[1],arcname='data');t.close()"
    )
    CommandRunner().stream_gzip([sys.executable, "-c", program, str(source)], archive, 30)
    size, digest = backup_identity(archive)
    assert size == archive.stat().st_size
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    with tarfile.open(archive, "r:gz") as packed:
        assert packed.extractfile("data/account.db").read() == payload


@pytest.mark.parametrize("compressed", [False, True])
def test_bot_recovery_accepts_old_tar_and_new_gzip(tmp_path, compressed):
    import tarfile

    from al1s.components.host.bot_driver import backup_identity
    from al1s.components.host.bot_proof import RuntimeProof
    from tests.test_bot_recovery_boundaries import fixture

    driver, deployment, identity, payload, state, snapshot = fixture(tmp_path)
    state["State"]["Health"]["Status"] = "unhealthy"
    source = tmp_path / "payload"
    source.write_bytes(b"restore-data")
    directory = driver.directory(identity)
    archive = directory / ("data.tar.gz" if compressed else "data.tar")
    with tarfile.open(archive, "w:gz" if compressed else "w") as packed:
        packed.add(source, arcname="payload")
    size, digest = backup_identity(archive)
    snapshot.update(phase="backed_up", data_backup_bytes=size, data_backup_sha256=digest)
    (directory / "snapshot.json").write_text(json.dumps(snapshot))
    original_run = deployment.runner.run.side_effect

    def run(args, *rest):
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": snapshot["before_id"]}])
        return original_run(args, *rest)

    deployment.runner.run.side_effect = run
    deployment.stop.side_effect = lambda _: state["State"].update(Running=False)
    deployment.change_image.side_effect = lambda _alias, _image: state.update(
        Image=snapshot["before_id"]
    )
    deployment.restore.side_effect = lambda _alias, running: state["State"].update(
        Running=running, Health={"Status": "healthy"}
    )
    driver.live_proof = lambda _: RuntimeProof.model_validate(snapshot["before_proof"])
    driver.identity_matches = lambda *_: True
    assert driver.recover(identity, payload, {}) == "failed_safe"
    deployment.change_image.assert_called_once_with("qq", snapshot["before_image"])


def test_bot_recovery_rejects_truncated_gzip(tmp_path):
    import hashlib
    import tarfile

    from tests.test_bot_recovery_boundaries import fixture

    driver, deployment, identity, payload, state, snapshot = fixture(tmp_path)
    state["State"]["Health"]["Status"] = "unhealthy"
    source = tmp_path / "payload"
    source.write_bytes(b"restore-data" * 1_000)
    directory = driver.directory(identity)
    archive = directory / "data.tar.gz"
    with tarfile.open(archive, "w:gz") as packed:
        packed.add(source, arcname="payload")
    truncated = archive.read_bytes()[:-8]
    archive.write_bytes(truncated)
    snapshot.update(phase="backed_up", data_backup_bytes=len(truncated),
                    data_backup_sha256=hashlib.sha256(truncated).hexdigest())
    (directory / "snapshot.json").write_text(json.dumps(snapshot))
    assert driver.recover(identity, payload, {}) == "unknown"
    deployment.stop.assert_not_called()
