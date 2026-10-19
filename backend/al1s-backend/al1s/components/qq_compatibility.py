"""Reviewed Direct/Linux data contract, not a blanket major-version claim.

Sources: 8.1.9 at 219921f0384366fa7fddd8949e0dd4247fd6872a and
8.3.2 at dc2b2b90092ef0f571b5546878288b83fff1e436. Config/defaults/types,
SQLite v3 models and GUID storage were compared; Linux session path and
bidirectional cipher fixtures passed. Evidence is kept in
artifacts/bot-service-updates/qq-data-compatibility.json.
"""

INDEXES = {
    "8.1.9": "sha256:5abd76aae928e46da490a48cf22dfe54dac4a19c4e334e6687bbc99fef1d17f0",
    "8.3.2": "sha256:0b9e20fdda15db223eceff87b0afd51c82dee156ce3c49424f1eb7f417786cdd",
}
SOURCES = {"8.1.9": "8.1.9,8.3.2", "8.3.2": "8.1.9,8.3.2"}


def compatible_qq_sources(version: str) -> str:
    return SOURCES.get(version, version)


def assert_qq_source_verified(version: str, index: str | None) -> None:
    if version not in INDEXES or INDEXES[version] != index:
        raise ValueError("qq_data_format_unverified")
