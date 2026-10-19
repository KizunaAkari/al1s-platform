"""Apply one saved device generation before any native QQ process starts."""

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from al1s.bots.qq_identity.contracts import (
    AppliedIdentity,
    IdentityModel,
    IdentityRecord,
    QqIdentityError,
)
from al1s.bots.qq_identity.repository import (
    QqIdentityRepository,
    atomic_bytes,
    digest,
    read_bytes,
    safe_file,
    sync_directory,
)

SESSION_PATTERN = r"qq-session-[0-9]+(?:-(?:linux|windows|macos|watch))?\.json"
MAX_FILE = 2 * 1024 * 1024


class BootstrapResult(IdentityModel):
    device_name: str | None = None
    blocked: bool = False
    error_code: str | None = None


class FileProof(IdentityModel):
    name: str = Field(pattern=rf"^(?:machine_guid\.bin|{SESSION_PATTERN})$")
    size: int = Field(ge=0, le=MAX_FILE)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApplyJournal(IdentityModel):
    generation: UUID
    target_guid: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase: Literal["preparing", "backed_up", "sessions_archived", "guid_applied", "complete"]
    files: list[FileProof] = Field(min_length=1, max_length=257)


def checkpoint(_stage: str) -> None:
    """Fault injection boundary; normal execution has no external callbacks."""


def default_device_name(guid: bytes, native_version: str) -> str:
    if native_version == "8.1.9":
        return "LuckyLillia-" + digest(guid)[:6]
    if native_version != "8.3.2":
        raise QqIdentityError("qq_device_native_version_unsupported", 422)
    value = int.from_bytes(hashlib.sha256(b"llbot-devname-v1" + guid).digest()[:8], "big")
    alphabet, suffix = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", ""
    for _ in range(7):
        suffix += alphabet[value % 36]
        value //= 36
    return "DESKTOP-" + suffix


def private_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise QqIdentityError("qq_identity_backup_invalid", 503)
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    sync_directory(path)
    sync_directory(path.parent)


def file_proof(path: Path) -> FileProof:
    safe_file(path)
    if path.stat().st_size > MAX_FILE:
        raise QqIdentityError("qq_identity_session_limit", 503)
    sha, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            size += len(chunk)
            if size > MAX_FILE:
                raise QqIdentityError("qq_identity_session_limit", 503)
            sha.update(chunk)
    return FileProof(name=path.name, size=size, sha256=sha.hexdigest())


def copy_verified(source: Path, destination: Path, proof: FileProof) -> None:
    safe_file(source)
    safe_file(destination)
    if destination.exists():
        if file_proof(destination) != proof:
            raise QqIdentityError("qq_identity_backup_invalid", 503)
        return
    temporary = destination.with_name(".copy-" + uuid4().hex)
    try:
        sha, size = hashlib.sha256(), 0
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            os.chmod(temporary, 0o600)
            for chunk in iter(lambda: incoming.read(65536), b""):
                size += len(chunk)
                if size > MAX_FILE:
                    raise QqIdentityError("qq_identity_session_limit", 503)
                sha.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if size != proof.size or sha.hexdigest() != proof.sha256:
            raise QqIdentityError("qq_identity_source_changed", 503)
        os.replace(temporary, destination)
        sync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def session_names(root: Path) -> set[str]:
    names = {p.name for p in root.iterdir() if re.fullmatch(SESSION_PATTERN, p.name)}
    if len(names) > 256:
        raise QqIdentityError("qq_identity_session_limit", 503)
    return names


def load_journal(repository: QqIdentityRepository) -> ApplyJournal | None:
    safe_file(repository.journal_path)
    if not repository.journal_path.exists():
        return None
    value = ApplyJournal.model_validate_json(read_bytes(repository.journal_path, 131072))
    if len({p.name for p in value.files}) != len(value.files):
        raise QqIdentityError("qq_identity_backup_invalid", 503)
    return value


def write_journal(repository: QqIdentityRepository, journal: ApplyJournal, phase: str) -> None:
    updated = ApplyJournal.model_validate({**journal.model_dump(), "phase": phase})
    journal.phase = updated.phase
    atomic_bytes(repository.journal_path, journal.model_dump_json().encode())


def prepare_journal(repository: QqIdentityRepository, record: IdentityRecord) -> ApplyJournal:
    if digest(repository.guid()) != record.source_guid_sha256 or record.request_id is None:
        raise QqIdentityError("qq_identity_source_changed", 503)
    names = sorted({"machine_guid.bin", *session_names(repository.root)})
    journal = ApplyJournal(
        generation=record.request_id,
        target_guid=record.desired.guid,
        source_sha256=record.source_guid_sha256,
        phase="preparing",
        files=[file_proof(repository.root / name) for name in names],
    )
    write_journal(repository, journal, "preparing")
    checkpoint("journal_prepared")
    return journal


def backup_paths(repository: QqIdentityRepository, journal: ApplyJournal) -> tuple[Path, Path]:
    base = repository.root / ".al1s-device-backups"
    private_directory(base)
    base /= str(journal.generation)
    private_directory(base)
    for name in ("copies", "retired"):
        private_directory(base / name)
    manifest = base / "manifest.json"
    expected = journal.model_copy(update={"phase": "preparing"}).model_dump_json().encode()
    safe_file(manifest)
    if manifest.exists():
        if read_bytes(manifest, 131072) != expected:
            raise QqIdentityError("qq_identity_backup_invalid", 503)
    else:
        atomic_bytes(manifest, expected)
    return base / "copies", base / "retired"


def protect_sessions(repository: QqIdentityRepository, journal: ApplyJournal) -> None:
    copies, retired = backup_paths(repository, journal)
    if journal.phase == "preparing":
        if digest(repository.guid()) != journal.source_sha256:
            raise QqIdentityError("qq_identity_source_changed", 503)
        for proof in journal.files:
            copy_verified(repository.root / proof.name, copies / proof.name, proof)
        write_journal(repository, journal, "backed_up")
        checkpoint("backup_verified")
    for proof in journal.files:
        if file_proof(copies / proof.name) != proof:
            raise QqIdentityError("qq_identity_backup_invalid", 503)
    if journal.phase != "backed_up":
        return
    if digest(repository.guid()) != journal.source_sha256:
        raise QqIdentityError("qq_identity_source_changed", 503)
    proofs = {proof.name: proof for proof in journal.files if proof.name != "machine_guid.bin"}
    if session_names(repository.root) - proofs.keys():
        raise QqIdentityError("qq_identity_sessions_changed", 503)
    for name, proof in proofs.items():
        source, target = repository.root / name, retired / name
        safe_file(source)
        safe_file(target)
        if target.exists():
            if source.exists() or file_proof(target) != proof:
                raise QqIdentityError("qq_identity_sessions_changed", 503)
        else:
            if file_proof(source) != proof:
                raise QqIdentityError("qq_identity_sessions_changed", 503)
            os.replace(source, target)
            sync_directory(repository.root)
            sync_directory(retired)
            checkpoint("session_retired")
    write_journal(repository, journal, "sessions_archived")
    checkpoint("sessions_archived")


def change_guid(repository: QqIdentityRepository, journal: ApplyJournal) -> None:
    current = repository.guid()
    if journal.phase == "sessions_archived":
        if session_names(repository.root):
            raise QqIdentityError("qq_identity_sessions_changed", 503)
        if digest(current) == journal.source_sha256:
            atomic_bytes(repository.root / "machine_guid.bin", bytes.fromhex(journal.target_guid))
            checkpoint("guid_replaced")
        elif current.hex() != journal.target_guid:
            raise QqIdentityError("qq_identity_source_changed", 503)
        write_journal(repository, journal, "guid_applied")
    if repository.guid().hex() != journal.target_guid:
        raise QqIdentityError("qq_identity_source_changed", 503)


def apply_locked(repository: QqIdentityRepository, native_version: str) -> BootstrapResult:
    guid = repository.guid()
    record = repository.load(guid)
    if record.desired.device_name and native_version != "8.3.2":
        raise QqIdentityError("qq_device_name_override_unsupported", 422)
    journal = load_journal(repository)
    resume = journal is not None and journal.phase != "complete"
    if guid.hex() != record.desired.guid or resume:
        if not resume:
            journal = prepare_journal(repository, record)
        assert journal is not None
        if journal.generation != record.request_id or journal.target_guid != record.desired.guid:
            raise QqIdentityError("qq_identity_apply_unresolved", 503)
        protect_sessions(repository, journal)
        change_guid(repository, journal)
    elif digest(guid) != record.source_guid_sha256:
        raise QqIdentityError("qq_identity_source_changed", 503)
    reported = record.desired.device_name or default_device_name(repository.guid(), native_version)
    if (record.applied is None or record.applied.settings != record.desired
            or record.applied.reported_device_name != reported
            or record.phase != "applied" or record.error_code):
        record.applied = AppliedIdentity(
            settings=record.desired, reported_device_name=reported, applied_at=datetime.now(UTC)
        )
        record.source_guid_sha256 = digest(repository.guid())
        record.phase, record.error_code = "applied", None
        record.version += 1
        repository.write(record)
        checkpoint("record_applied")
    if journal is not None and journal.phase != "complete":
        write_journal(repository, journal, "complete")
        checkpoint("complete")
    return BootstrapResult(device_name=record.desired.device_name or None)


def apply_identity(repository: QqIdentityRepository, native_version: str) -> BootstrapResult:
    try:
        with repository.exclusive():
            try:
                return apply_locked(repository, native_version)
            except (OSError, ValueError) as error:
                code = (error.code if isinstance(error, QqIdentityError)
                        else "qq_identity_apply_failed")
                try:
                    record = repository.load(repository.guid())
                    record.error_code, record.phase = code, "failed"
                    record.version += 1
                    repository.write(record)
                except (OSError, ValueError):
                    pass
                return BootstrapResult(blocked=True, error_code=code)
    except (OSError, ValueError):
        return BootstrapResult(blocked=True, error_code="qq_identity_busy_or_io_failure")
