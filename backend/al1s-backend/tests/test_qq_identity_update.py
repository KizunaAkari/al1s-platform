from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.bots.qq_identity.contracts import QqDeviceSettings, QqIdentitySave, QqIdentitySummary
from al1s.bots.qq_identity.repository import QqIdentityRepository
from al1s.components.contracts import ComponentInventoryEntry, ComponentRelease, ComponentTarget
from al1s.components.host.bot_proof import RuntimeProof
from al1s.components.host.qq_identity_summary import read_qq_identity_summary
from al1s.components.operations import compatible_release
from tests.test_bot_upgrade_driver import upgrade_payload


def test_preflight_rejects_pending_and_legacy_identity_candidates():
    _, payload = upgrade_payload()
    release = ComponentRelease.model_validate(payload["release"])
    current = ComponentInventoryEntry(
        component="qq-bot", upgrade_unit="qq-bot", target=ComponentTarget(kind="bot", bot_alias="qq"),
        display_name="QQ", version="8.1.9", architecture="amd64", status="healthy",
        source_identity="a" * 64, qq_identity=QqIdentitySummary(configured=True, pending=True),
    )
    assert "qq_identity_pending_restart" in compatible_release(current, release)
    current = current.model_copy(update={"qq_identity": QqIdentitySummary(configured=True)})
    assert "qq_identity_runtime_unsupported" in compatible_release(current, release)
    upgraded = release.model_copy(update={"runtime_version": "1.1.0"})
    assert "qq_identity_runtime_unsupported" not in compatible_release(current, upgraded)
    assert "qq_identity_runtime_unsupported" not in compatible_release(
        current, upgraded.model_copy(update={"runtime_version": "1.2.0"})
    )
    current = current.model_copy(update={"qq_identity": QqIdentitySummary(configured=True, custom_name=True)})
    assert "qq_device_name_override_unsupported" in compatible_release(current, upgraded)
    assert not compatible_release(current.model_copy(update={"qq_identity": None}), release)


def test_summary_is_read_only_bounded_and_bound_to_registered_qq_mount(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "machine_guid.bin").write_bytes(bytes.fromhex("12" * 16))
    repository = QqIdentityRepository(root)
    repository.save(QqIdentitySave(settings=QqDeviceSettings(device_name="mine", guid="12" * 16)),
                    repository.read().revision, uuid4())
    deployment = MagicMock()
    deployment.settings = SimpleNamespace(deployment_root=tmp_path)
    deployment.config.return_value = {"services": {"llbot": {"volumes": [
        {"type": "bind", "source": str(root), "target": "/app/llbot/data"},
    ]}}}
    deployment.inspect.return_value = {"Mounts": [
        {"Type": "bind", "Source": str(root), "Destination": "/app/llbot/data"},
    ]}
    before = repository.path.read_bytes()
    summary = read_qq_identity_summary(deployment)
    assert summary.configured and summary.pending and summary.custom_name
    assert repository.path.read_bytes() == before
    deployment.inspect.return_value["Mounts"][0]["Source"] = str(tmp_path / "other")
    with pytest.raises(ValueError, match="mount"):
        read_qq_identity_summary(deployment)


def test_old_proof_remains_parseable_and_cannot_claim_identity_capability():
    proof = RuntimeProof(role="qq", native_version="8.1.9", runtime_version="1.0.0", render_proof=True)
    assert not proof.qq_device_identity_v1 and not proof.qq_device_name_override


def test_ops_reads_root_private_metadata_without_relaxing_file_permissions(tmp_path, monkeypatch):
    from al1s.components.host import qq_identity_summary
    root = tmp_path / "data"
    root.mkdir()
    (root / "machine_guid.bin").write_bytes(bytes.fromhex("12" * 16))
    repository = QqIdentityRepository(root)
    repository.save(QqIdentitySave(settings=QqDeviceSettings(device_name="mine", guid="12" * 16)),
                    repository.read().revision, uuid4())
    deployment = MagicMock()
    deployment.settings = SimpleNamespace(deployment_root=tmp_path)
    deployment.config.return_value = {"services": {"llbot": {"volumes": [
        {"type": "bind", "source": str(root), "target": "/app/llbot/data"}]}}}
    deployment.inspect.return_value = {"Id": "a" * 64, "Mounts": [
        {"Type": "bind", "Source": str(root), "Destination": "/app/llbot/data"}]}
    def denied(_root):
        raise PermissionError()
    monkeypatch.setattr(qq_identity_summary, "stored_summary", denied)
    monkeypatch.setattr(qq_identity_summary, "archive_metadata_file",
                        lambda instance, name: (root / name).read_bytes(), raising=False)
    result = read_qq_identity_summary(deployment)
    assert result.pending and result.configured and result.custom_name


@pytest.mark.parametrize("mode", ["valid", "symlink", "oversized", "wrong_name"])
def test_metadata_archive_reads_only_bounded_regular_whitelisted_file(monkeypatch, mode):
    import io
    import tarfile
    from al1s.components.host import qq_identity_summary

    data = bytes.fromhex("12" * 16)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        header = tarfile.TarInfo("machine_guid.bin" if mode != "wrong_name" else "other-file")
        header.size = 17 if mode == "oversized" else 16
        if mode == "symlink":
            header.type, header.linkname, header.size = tarfile.SYMTYPE, "elsewhere", 0
        archive.addfile(header, io.BytesIO(data + b"x"))
    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = buffer.getvalue()
    monkeypatch.setattr(qq_identity_summary, "UnixConnection", lambda *_args: connection)
    if mode == "valid":
        assert qq_identity_summary.archive_metadata_file("a" * 64, "machine_guid.bin") == data
    else:
        with pytest.raises(ValueError, match="archive_invalid"):
            qq_identity_summary.archive_metadata_file("a" * 64, "machine_guid.bin")
    with pytest.raises(ValueError, match="path_invalid"):
        qq_identity_summary.archive_metadata_file("a" * 64, "../session")


@pytest.mark.parametrize("failure", ["changed", "missing", "pending", "failed", "legacy"])
def test_upgrade_verify_rejects_lost_or_unapplied_device_configuration(tmp_path, failure):
    import json
    from al1s.meme_workshop.operation_identity import OperationIdentity
    from tests.test_bot_recovery_boundaries import fixture

    driver, _, identity, payload, _, snapshot = fixture(tmp_path)
    payload["release"]["native_version"] = "8.3.2"
    before = QqIdentitySummary(configured=True, custom_name=True, configuration_sha256="a" * 64)
    snapshot["operational_identity"]["qq_identity"] = before.model_dump()
    (driver.directory(identity) / "snapshot.json").write_text(json.dumps(snapshot))
    observed = (None if failure == "missing" else before.model_copy(update={
        "configuration_sha256": "b" * 64 if failure == "changed" else "a" * 64,
        "pending": failure == "pending", "failed": failure == "failed",
    }))
    driver.live_identity = lambda *_args: OperationIdentity.model_validate({
        **snapshot["operational_identity"], "qq_identity": observed,
    })
    driver.live_proof = lambda *_args: RuntimeProof(
        role="qq", native_version="8.3.2", runtime_version="1.1.0", render_proof=True,
        qq_device_identity_v1=failure != "legacy", qq_device_name_override=failure != "legacy",
    )
    assert not driver.verify(identity, payload, {})
