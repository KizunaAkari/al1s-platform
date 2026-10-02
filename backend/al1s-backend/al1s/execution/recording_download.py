from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from al1s.execution.errors import InvalidRequestError, NotFoundError

MAX_RECORDING_RANGE = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RecordingFile:
    file_name: str
    sha256: str
    size_bytes: int
    object_key: str


@dataclass(frozen=True, slots=True)
class RecordingSummary:
    artifact_id: UUID
    attempt_id: UUID
    file_name: str
    status: str
    downloadable: bool


@dataclass(frozen=True, slots=True)
class RecordingPage:
    items: tuple[RecordingSummary, ...]
    next_cursor: UUID | None


class RecordingReader(Protocol):
    def list_recordings(
        self, task_id: UUID, now: datetime, cursor: UUID | None, limit: int
    ) -> RecordingPage: ...

    def find_downloadable(
        self, task_id: UUID, artifact_id: UUID, now: datetime
    ) -> RecordingFile | None: ...


class RecordingStore(Protocol):
    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes: ...


class RecordingDownloadService:
    def __init__(
        self,
        reader: RecordingReader,
        store: RecordingStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader = reader
        self._store = store
        self._clock = clock

    def metadata(self, task_id: UUID, artifact_id: UUID) -> RecordingFile:
        item = self._reader.find_downloadable(task_id, artifact_id, self._clock())
        if item is None:
            raise NotFoundError("task_recording")
        return item

    def list_recordings(
        self, task_id: UUID, cursor: UUID | None = None, limit: int = 20
    ) -> RecordingPage:
        if not 1 <= limit <= 50:
            raise InvalidRequestError("invalid_recording_limit", "Limit must be 1 to 50")
        return self._reader.list_recordings(task_id, self._clock(), cursor, limit)

    def stream(self, task_id: UUID, artifact_id: UUID, expected: RecordingFile) -> Iterator[bytes]:
        for start in range(0, expected.size_bytes, MAX_RECORDING_RANGE):
            end = min(start + MAX_RECORDING_RANGE, expected.size_bytes) - 1
            current, body = self.read_range(task_id, artifact_id, start, end)
            if current != expected:
                raise RuntimeError("Recording changed during download")
            yield body

    def read_range(
        self, task_id: UUID, artifact_id: UUID, start: int, end: int
    ) -> tuple[RecordingFile, bytes]:
        item = self.metadata(task_id, artifact_id)
        if start < 0 or end < start or end >= item.size_bytes:
            raise InvalidRequestError("invalid_recording_range", "Invalid recording byte range")
        size = end - start + 1
        if size > MAX_RECORDING_RANGE:
            raise InvalidRequestError("recording_range_too_large", "Maximum range is 4 MiB")
        body = self._store.get_range(item.object_key, start, end)
        if len(body) != size:
            raise RuntimeError("Recording store returned an invalid range length")
        return item, body
