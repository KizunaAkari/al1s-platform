import hashlib

import pytest

from al1s.maa.archive_format import canonical_json, logical_payload


@pytest.mark.parametrize(
    "version, digest",
    [
        (1, "873f7ceb0b027de7e8c30dd2879af5558245bf21bf6957077186c8e00c6fd8f6"),
        (2, "89c5639ffc706ef211fa0438314f89bef2a365b0d489deb3e140e04b5758cc22"),
        (3, "06c40e7228f91fd74ffccd26286ce3a1cb1c130734485a572c92f18c38c70a52"),
    ],
)
def test_digest_matches_fixed_pre_refactor_format(version, digest):
    manifest = {
        "schema": f"al1s-script-archive/v{version}",
        "source": {"scope": "category", "ignored": "metadata"},
        "categories_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "statistics": {"scripts": 1},
        "scripts": [{"name": "中文", "sha256": "c" * 64}],
        "resources": [],
        "strategy": {"name": "组合", "modules": ["main"]},
    }
    assert hashlib.sha256(canonical_json(logical_payload(manifest))).hexdigest() == digest
    manifest["source"]["ignored"] = "different transport metadata"
    assert hashlib.sha256(canonical_json(logical_payload(manifest))).hexdigest() == digest
    manifest["scripts"][0]["name"] = "changed"
    assert hashlib.sha256(canonical_json(logical_payload(manifest))).hexdigest() != digest
