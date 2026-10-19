import json

import pytest

from al1s.meme_workshop.runtime_identity import read_runtime_identity


def test_identity_is_read_from_the_installed_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "role": "qq",
                "runtime_version": "1.0.0",
                "source_sha256": "a" * 64,
                "requirements_sha256": "b" * 64,
            }
        )
    )
    assert read_runtime_identity(path).role == "qq"


def test_invalid_identity_never_guesses_a_version(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"role":"other","runtime_version":"latest"}')
    with pytest.raises(ValueError):
        read_runtime_identity(path)
