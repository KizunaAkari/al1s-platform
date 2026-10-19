from pathlib import Path

from al1s.bots.qq_identity.contracts import IdentityRecord, QqIdentitySummary
from al1s.bots.qq_identity.repository import QqIdentityRepository, digest


def stored_summary(root: Path) -> QqIdentitySummary:
    repository = QqIdentityRepository(root)
    if not repository.path.exists():
        return QqIdentitySummary()
    guid = repository.guid()
    return summarize_record(repository.load(guid), guid)


def summarize_record(record: IdentityRecord, guid: bytes) -> QqIdentitySummary:
    if len(guid) != 16 or guid == bytes(16):
        raise ValueError("qq_identity_guid_invalid")
    return QqIdentitySummary(
        configured=record.configured,
        pending=record.configured and (record.phase in {"pending", "failed"}
                or record.applied is None or record.desired != record.applied.settings),
        failed=record.error_code is not None or record.source_guid_sha256 != digest(guid),
        custom_name=bool(record.desired.device_name),
        configuration_sha256=digest(record.desired.model_dump_json().encode())
        if record.configured else None,
    )
