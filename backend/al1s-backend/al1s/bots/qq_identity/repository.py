import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from al1s.bots.qq_identity.contracts import (
    IdentityRecord,
    QqDeviceSettings,
    QqIdentityError,
    QqIdentitySave,
    QqIdentityState,
)
from al1s.components.host.journal import HostJournal

RECORD_NAME = ".al1s-qq-identity.json"
JOURNAL_NAME = ".al1s-qq-identity-apply.json"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_file(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise QqIdentityError("qq_identity_file_invalid", 503)


def read_bytes(path: Path, maximum: int) -> bytes:
    safe_file(path)
    with path.open("rb") as source:
        value = source.read(maximum + 1)
    if len(value) > maximum:
        raise QqIdentityError("qq_identity_record_invalid", 503)
    return value


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_bytes(path: Path, data: bytes) -> None:
    safe_file(path)
    temporary = path.with_name(".identity-" + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as target:
            os.chmod(temporary, 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class QqIdentityRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / RECORD_NAME
        self.journal_path = self.root / JOURNAL_NAME

    @contextmanager
    def exclusive(self) -> Any:
        directory = self.root / ".al1s-qq-identity-lock"
        if directory.is_symlink():
            raise QqIdentityError("qq_identity_file_invalid", 503)
        try:
            with HostJournal(directory).exclusive("bot:qq"):
                yield
        except OSError as error:
            raise QqIdentityError("qq_identity_busy_or_io_failure", 503) from error

    def guid(self) -> bytes:
        value = read_bytes(self.root / "machine_guid.bin", 16)
        if len(value) != 16 or value == bytes(16):
            raise QqIdentityError("qq_device_guid_invalid", 503)
        return value

    def load(self, guid: bytes) -> IdentityRecord:
        safe_file(self.path)
        if not self.path.exists():
            return IdentityRecord(
                desired=QqDeviceSettings(device_name="", guid=guid.hex()),
                source_guid_sha256=digest(guid),
            )
        try:
            return IdentityRecord.model_validate_json(read_bytes(self.path, 32768))
        except ValueError as error:
            raise QqIdentityError("qq_identity_record_invalid", 503) from error

    def write(self, record: IdentityRecord) -> None:
        atomic_bytes(self.path, record.model_dump_json().encode())

    def project(self, record: IdentityRecord, guid: bytes) -> QqIdentityState:
        revision = digest(record.model_dump_json().encode() + guid)
        pending = record.configured and (
            record.phase in {"pending", "failed"}
            or record.applied is None or record.desired != record.applied.settings
        )
        return QqIdentityState(
            mode="pmhq" if os.environ.get("PROTOCOL_MODE") == "pmhq" else "direct",
            available=os.environ.get("PROTOCOL_MODE") != "pmhq",
            unavailable_reason="qq_device_settings_direct_only"
            if os.environ.get("PROTOCOL_MODE") == "pmhq" else None,
            revision=revision,
            configured=record.configured,
            desired=record.desired,
            applied=record.applied,
            current_guid=guid.hex(),
            pending=pending,
            phase=record.phase,
            error_code=record.error_code,
            observed_at=datetime.now(UTC),
        )

    def read(self) -> QqIdentityState:
        guid = self.guid()
        return self.project(self.load(guid), guid)

    def save(
        self, body: QqIdentitySave, expected_revision: str, request_id: UUID
    ) -> QqIdentityState:
        if os.environ.get("PROTOCOL_MODE") == "pmhq":
            raise QqIdentityError("qq_device_settings_direct_only", 409)
        with self.exclusive():
            return self._save(body, expected_revision, request_id)

    def _save(
        self, body: QqIdentitySave, expected_revision: str, request_id: UUID
    ) -> QqIdentityState:
        guid = self.guid()
        record = self.load(guid)
        request_hash = digest(body.model_dump_json().encode())
        if request_id == record.request_id:
            if request_hash != record.request_sha256:
                raise QqIdentityError("qq_identity_conflict")
            return self.project(record, guid)
        if expected_revision != self.project(record, guid).revision:
            raise QqIdentityError("qq_identity_revision_changed")
        if self.journal_path.exists():
            journal = json.loads(read_bytes(self.journal_path, 131072))
            if journal.get("phase") != "complete":
                raise QqIdentityError("qq_identity_apply_unresolved")
        if body.settings.guid != guid.hex() and not body.confirm_guid_change:
            raise QqIdentityError("qq_guid_confirmation_required")
        record.version += 1
        record.configured = True
        record.desired = body.settings
        record.source_guid_sha256 = digest(guid)
        record.request_id, record.request_sha256 = request_id, request_hash
        record.phase, record.error_code = "pending", None
        self.write(record)
        return self.project(record, guid)
