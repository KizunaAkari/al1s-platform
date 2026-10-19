from uuid import uuid4

import pytest
from pydantic import ValidationError

from al1s.bots.qq_identity.contracts import QqDeviceSettings, QqIdentityError, QqIdentitySave
from al1s.bots.qq_identity.repository import QqIdentityRepository


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "machine_guid.bin").write_bytes(bytes.fromhex("12" * 16))
    (tmp_path / "qq-session-123-linux.json").write_bytes(b"original-session")
    return QqIdentityRepository(tmp_path)


def body(guid="12" * 16, name="My QQ", confirmed=False):
    return QqIdentitySave(
        settings=QqDeviceSettings(device_name=name, guid=guid), confirm_guid_change=confirmed
    )


def test_normalize_guid_and_name():
    value = QqDeviceSettings(device_name="  我的设备  ", guid="12345678-ABCD-1234-ABCD-123456789ABC")
    assert value.guid == "12345678abcd1234abcd123456789abc"
    assert value.device_name == "我的设备"
    for guid in ("0" * 32, "random", "1" * 31, "{" + "1" * 32 + "}"):
        with pytest.raises(ValidationError):
            QqDeviceSettings(device_name="", guid=guid)
    for name in ("x\nname", "x\x00name", "x" * 65):
        with pytest.raises(ValidationError):
            QqDeviceSettings(device_name=name, guid="12" * 16)


def test_save_does_not_change_native_identity(repository):
    before = repository.read()
    saved = repository.save(body(), before.revision, uuid4())
    assert saved.pending
    assert saved.desired.device_name == "My QQ"
    assert (repository.root / "machine_guid.bin").read_bytes() == bytes.fromhex("12" * 16)
    assert (repository.root / "qq-session-123-linux.json").read_bytes() == b"original-session"


def test_cas_and_idempotency(repository):
    before, request = repository.read(), uuid4()
    saved = repository.save(body(), before.revision, request)
    assert repository.save(body(), before.revision, request).revision == saved.revision
    with pytest.raises(QqIdentityError, match="identity_conflict"):
        repository.save(body(name="Other"), before.revision, request)
    with pytest.raises(QqIdentityError, match="revision_changed"):
        repository.save(body(name="Other"), before.revision, uuid4())
    assert repository.read().desired.device_name == "My QQ"


def test_guid_change_requires_explicit_confirmation(repository):
    with pytest.raises(QqIdentityError, match="guid_confirmation_required"):
        repository.save(body(guid="34" * 16), repository.read().revision, uuid4())
    saved = repository.save(body(guid="34" * 16, confirmed=True), repository.read().revision, uuid4())
    assert saved.pending
    assert (repository.root / "machine_guid.bin").read_bytes().hex() == "12" * 16


def test_external_guid_change_invalidates_revision(repository):
    old = repository.read()
    (repository.root / "machine_guid.bin").write_bytes(bytes.fromhex("56" * 16))
    with pytest.raises(QqIdentityError, match="revision_changed"):
        repository.save(body(), old.revision, uuid4())


def test_oversized_record_is_rejected(repository):
    repository.path.write_bytes(b"x" * 32769)
    with pytest.raises(QqIdentityError, match="record_invalid"):
        repository.read()
