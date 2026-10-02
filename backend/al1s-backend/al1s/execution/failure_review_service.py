from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

from al1s.execution.recording_download import MAX_RECORDING_RANGE, RecordingFile, RecordingStore


class FailureReviewRepository(Protocol):
    def list_attempts(
        self, task_id: UUID, cursor: UUID | None, now: datetime
    ) -> dict[str, Any]: ...
    def metadata(
        self, task: UUID, attempt: UUID, artifact: UUID, now: datetime
    ) -> RecordingFile: ...
    def downloaded(self, task: UUID, attempt: UUID, artifact: UUID, now: datetime) -> None: ...
    def confirm(self, task: UUID, attempt: UUID, now: datetime) -> None: ...


class FailureReviewService:
    def __init__(
        self,
        repository: FailureReviewRepository,
        store: RecordingStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository, self.store, self.clock = repository, store, clock

    def details(self, task: UUID, cursor: UUID | None) -> dict[str, Any]:
        return self.repository.list_attempts(task, cursor, self.clock())

    def metadata(self, task: UUID, attempt: UUID, artifact: UUID) -> RecordingFile:
        return self.repository.metadata(task, attempt, artifact, self.clock())

    def stream(
        self,
        task: UUID,
        attempt: UUID,
        artifact: UUID,
        expected: RecordingFile,
        *,
        preview: bool = False,
    ) -> Iterator[bytes]:
        digest = sha256()
        for start in range(0, expected.size_bytes, MAX_RECORDING_RANGE):
            if self.metadata(task, attempt, artifact) != expected:
                raise RuntimeError("Screenshot changed during download")
            end = min(start + MAX_RECORDING_RANGE, expected.size_bytes) - 1
            body = self.store.get_range(expected.object_key, start, end)
            if len(body) != end - start + 1:
                raise RuntimeError("Truncated screenshot")
            digest.update(body)
            yield body
        if digest.hexdigest() != expected.sha256:
            raise RuntimeError("Screenshot digest mismatch")
        # Generator cancellation/disconnection must not reach this receipt.
        if not preview:
            self.repository.downloaded(task, attempt, artifact, self.clock())

    def confirm(self, task: UUID, attempt: UUID) -> None:
        self.repository.confirm(task, attempt, self.clock())
