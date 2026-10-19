import json

from al1s.meme_workshop.runtime_probe import prove_runtime


def test_probe_checks_actual_package_and_renders_without_runtime_network(tmp_path):
    manifest = tmp_path / "manifest.json"
    package = tmp_path / "package.json"
    manifest.write_text(
        json.dumps(
            dict(
                role="qq",
                native_version="8.1.9",
                runtime_version="1.0.0",
                source_sha256="a" * 64,
                requirements_sha256="b" * 64,
            )
        )
    )
    package.write_text('{"version":"8.1.9"}')
    result = prove_runtime(manifest, package, "qq")
    assert result["native_version"] == "8.1.9"
    assert result["render_proof"] is True
    package.write_text('{"version":"8.3.2"}')
    import pytest

    with pytest.raises(ValueError, match="native_version"):
        prove_runtime(manifest, package, "qq")
