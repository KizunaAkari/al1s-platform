import json
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

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
