from pathlib import Path

import pytest
from pydantic import ValidationError

from al1s.bots.qq_headed.profile import (
    prepare_client_profile,
    read_client_profile,
    validate_client_version,
)
from al1s.meme_workshop.runtime_identity import RuntimeIdentity


def manifest(**extra):
    return dict(role="qq", runtime_version="2.0.0", native_version="8.3.2",
                source_sha256="a" * 64, requirements_sha256="b" * 64, **extra)


def test_manifest_defaults_to_direct_and_requires_headed_versions():
    assert RuntimeIdentity(**manifest()).qq_protocol_mode == "direct"
    with pytest.raises(ValidationError):
        RuntimeIdentity(**manifest(qq_protocol_mode="pmhq"))
    with pytest.raises(ValidationError):
        RuntimeIdentity(**manifest(qq_protocol_mode="other"))
    assert RuntimeIdentity(**manifest(qq_protocol_mode="pmhq", qq_client_version="3.2.25",
                                     pmhq_version="1.0.0", qq_headed_v1=True)).qq_headed_v1


def test_client_identity_is_persistent_and_account_scoped(tmp_path: Path):
    original = tmp_path / "machine_guid.bin"
    original.write_bytes(b"x" * 16)
    profile = prepare_client_profile(tmp_path, "123456")
    assert prepare_client_profile(tmp_path, "123456") == profile
    assert read_client_profile(tmp_path) == profile
    assert original.read_bytes() == b"x" * 16
    with pytest.raises(ValueError, match="account"):
        prepare_client_profile(tmp_path, "654321")


def test_invalid_record_is_not_regenerated(tmp_path: Path):
    prepare_client_profile(tmp_path, "123456")
    record = tmp_path / "qq-client" / ".al1s-client-profile.json"
    record.write_text("broken")
    with pytest.raises(ValueError):
        prepare_client_profile(tmp_path, "123456")
    assert record.read_text() == "broken"


def test_rejects_non_directory_and_invalid_account(tmp_path: Path):
    (tmp_path / "qq-client").write_text("not a directory")
    with pytest.raises(ValueError):
        prepare_client_profile(tmp_path, "123456")
    with pytest.raises(ValueError):
        prepare_client_profile(tmp_path, "../123")


def test_persisted_client_selector_cannot_silently_change_build(tmp_path: Path):
    prepare_client_profile(tmp_path, "123456")
    validate_client_version(tmp_path, "3.2.34-53644")
    folder = tmp_path / "qq-client" / "versions"
    folder.mkdir()
    (folder / "config.json").write_text('{"baseVersion":"3.2.34-53644", "curVersion":"3.2.99"}')
    with pytest.raises(ValueError, match="version"):
        validate_client_version(tmp_path, "3.2.34-53644")
