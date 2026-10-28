from uuid import uuid4
import json

import pytest

from al1s.bots.qq_identity import apply, startup
from al1s.bots.qq_identity.contracts import QqDeviceSettings, QqIdentitySave
from al1s.bots.qq_identity.repository import QqIdentityRepository


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "machine_guid.bin").write_bytes(bytes.fromhex("12" * 16))
    for name in ("qq-session-123.json", "qq-session-456-linux.json", "qq-session-456-watch.json"):
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / "config_123.json").write_bytes(b"account-config")
    return QqIdentityRepository(tmp_path)


def save(repository, guid, name="My QQ"):
    repository.save(
        QqIdentitySave(
            settings=QqDeviceSettings(device_name=name, guid=guid), confirm_guid_change=True
        ),
        repository.read().revision,
        uuid4(),
    )


def test_unchanged_first_boot_preserves_identity(repository):
    before = {p.name: p.read_bytes() for p in repository.root.iterdir() if p.is_file()}
    result = apply.apply_identity(repository, "8.3.2")
    assert not result.blocked and result.device_name is None
    assert repository.read().applied.reported_device_name.startswith("DESKTOP-")
    assert not repository.read().configured
    for name, data in before.items():
        assert (repository.root / name).read_bytes() == data


def test_name_only_preserves_all_sessions(repository):
    before = {p.name: p.read_bytes() for p in repository.root.glob("qq-session-*")}
    save(repository, "12" * 16)
    result = apply.apply_identity(repository, "8.3.2")
    assert not result.blocked and result.device_name == "My QQ"
    assert not repository.read().pending
    assert repository.read().applied.reported_device_name == "My QQ"
    assert repository.guid().hex() == "12" * 16
    for name, data in before.items():
        assert (repository.root / name).read_bytes() == data


def test_guid_change_archives_all_protocol_sessions(repository):
    save(repository, "34" * 16)
    result = apply.apply_identity(repository, "8.3.2")
    assert not result.blocked
    assert repository.guid().hex() == "34" * 16
    assert not list(repository.root.glob("qq-session-*.json"))
    assert len(list(repository.root.glob(".al1s-device-backups/*/retired/qq-session-*.json"))) == 3
    assert len(list(repository.root.glob(".al1s-device-backups/*/copies/machine_guid.bin"))) == 1
    assert (repository.root / "config_123.json").read_bytes() == b"account-config"
    (repository.root / "qq-session-123-linux.json").write_bytes(b"new-login")
    assert not apply.apply_identity(repository, "8.3.2").blocked
    assert (repository.root / "qq-session-123-linux.json").read_bytes() == b"new-login"


@pytest.mark.parametrize("point", [
    "journal_prepared", "backup_verified", "session_retired", "sessions_archived",
    "guid_replaced", "record_applied", "complete",
])
def test_interrupted_boot_reconciles_same_guid_without_losing_sessions(repository, monkeypatch, point):
    originals = {p.name: p.read_bytes() for p in repository.root.glob("qq-session-*")}
    save(repository, "34" * 16)

    def interrupted(stage):
        if stage == point:
            raise SystemExit("simulated power loss")

    with monkeypatch.context() as change:
        change.setattr(apply, "checkpoint", interrupted)
        with pytest.raises(SystemExit):
            apply.apply_identity(repository, "8.3.2")
    assert not apply.apply_identity(repository, "8.3.2").blocked
    assert repository.guid().hex() == "34" * 16
    for name, data in originals.items():
        copies = list(repository.root.glob(f".al1s-device-backups/*/copies/{name}"))
        assert len(copies) == 1 and copies[0].read_bytes() == data
    assert not repository.read().pending


def test_backup_failure_and_external_source_change_block_native_start(repository, monkeypatch):
    save(repository, "34" * 16)
    original = apply.copy_verified
    monkeypatch.setattr(apply, "copy_verified", lambda *_args: (_ for _ in ()).throw(OSError()))
    assert apply.apply_identity(repository, "8.3.2").blocked
    assert repository.guid().hex() == "12" * 16
    assert len(list(repository.root.glob("qq-session-*.json"))) == 3
    monkeypatch.setattr(apply, "copy_verified", original)
    (repository.root / "machine_guid.bin").write_bytes(bytes.fromhex("56" * 16))
    assert apply.apply_identity(repository, "8.3.2").blocked
    assert len(list(repository.root.glob("qq-session-*.json"))) == 3


def test_child_restart_does_not_apply_pending(repository):
    assert not apply.apply_identity(repository, "8.3.2").blocked
    save(repository, "34" * 16)
    # Native command selection consumes only the environment frozen by container bootstrap.
    assert startup.native_command({}) == ["/startup.sh"]
    assert startup.native_command({"AL1S_QQ_IDENTITY_BLOCKED": "1"}) is None
    assert repository.guid().hex() == "12" * 16 and repository.read().pending


def test_each_guid_generation_keeps_its_verified_recovery_manifest(repository):
    for guid in ("34" * 16, "56" * 16):
        save(repository, guid)
        assert not apply.apply_identity(repository, "8.3.2").blocked
        (repository.root / "qq-session-123-linux.json").write_bytes(guid.encode())
    manifests = list(repository.root.glob(".al1s-device-backups/*/manifest.json"))
    assert len(manifests) == 2
    for path in manifests:
        value = json.loads(path.read_text())
        assert value["source_sha256"]
        assert {p["name"] for p in value["files"]} >= {"machine_guid.bin"}


def test_backup_directory_links_are_synced_before_retiring_originals(repository, monkeypatch):
    from pathlib import Path
    import os

    synced = []
    monkeypatch.setattr(apply, "sync_directory", lambda path: synced.append(path))
    replace = os.replace

    def observed_replace(source, destination):
        source, destination = Path(source), Path(destination)
        if source.parent == repository.root and source.name.startswith("qq-session-"):
            assert repository.root / ".al1s-device-backups" in synced
            assert destination.parent.parent in synced
        replace(source, destination)

    save(repository, "34" * 16)
    monkeypatch.setattr(os, "replace", observed_replace)
    assert not apply.apply_identity(repository, "8.3.2").blocked
