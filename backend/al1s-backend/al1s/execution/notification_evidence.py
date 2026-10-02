"""Execution-owned evidence lookup; only ephemeral image bytes cross to notification sending."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from al1s.kernel.ports import BlobStore


class EvidencePending(RuntimeError):
    pass


@dataclass(frozen=True)
class NotificationEvidence:
    status: str  # ready / pending / unavailable
    object_key: str = ""
    size_bytes: int = 0
    sha256: str = ""


class NotificationEvidenceReader(Protocol):
    def read(
        self, task: UUID, execution: UUID, attempt: UUID, terminal: UUID, capture: UUID
    ) -> NotificationEvidence: ...


class ExecutionNotificationEvidence:
    def __init__(self, reader: NotificationEvidenceReader, store: BlobStore):
        self._reader, self._store = reader, store

    def read(self, payload: dict[str, object]) -> bytes | None:
        if not payload.get("capture_id"):
            return None
        try:
            identities = [
                UUID(str(payload.get(key)))
                for key in ("task_id", "execution_id", "attempt_id", "terminal_id", "capture_id")
            ]
        except ValueError:
            return None
        evidence = self._reader.read(*identities)
        if evidence.status == "pending":
            raise EvidencePending()
        if evidence.status != "ready":
            return None
        if not 0 < evidence.size_bytes <= 16 * 1024 * 1024:
            raise ValueError("Notification image exceeds size limit")
        body = bytearray()
        for start in range(0, evidence.size_bytes, 4 * 1024 * 1024):
            end = min(start + 4 * 1024 * 1024, evidence.size_bytes) - 1
            chunk = self._store.get_range(evidence.object_key, start, end)
            if len(chunk) != end - start + 1:
                raise ValueError("Notification image truncated")
            body.extend(chunk)
        if sha256(body).hexdigest() != evidence.sha256 or not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Notification image integrity mismatch")
        # Recheck permission after object I/O; confirmed/deleted evidence is not sent.
        current = self._reader.read(*identities)
        return bytes(body) if current == evidence else None
