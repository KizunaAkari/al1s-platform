from uuid import uuid4

import pytest

from al1s.bots.qq_identity.contracts import QqDeviceSettings, QqIdentityError, QqIdentitySave
from al1s.bots.qq_identity.repository import QqIdentityRepository


def test_pmhq_projects_preserved_direct_settings_but_rejects_writes(tmp_path, monkeypatch):
    (tmp_path / "machine_guid.bin").write_bytes(b"x" * 16)
    repository = QqIdentityRepository(tmp_path)
    before = repository.read()
    monkeypatch.setenv("PROTOCOL_MODE", "pmhq")
    projected = repository.read()
    assert projected.mode == "pmhq" and not projected.available
    assert projected.unavailable_reason == "qq_device_settings_direct_only"
    assert projected.current_guid == before.current_guid
    with pytest.raises(QqIdentityError, match="direct_only"):
        repository.save(QqIdentitySave(settings=QqDeviceSettings(device_name="new", guid="12"*16)),
                        before.revision, uuid4())
    assert (tmp_path / "machine_guid.bin").read_bytes() == b"x"*16
    assert not (tmp_path / ".al1s-qq-identity.json").exists()
