from __future__ import annotations

from types import TracebackType
from typing import Protocol
from uuid import UUID

from al1s.kernel.types import BlobRecord


class TerminalBlobAccessRepository(Protocol):
    def find_authorized(self, terminal_id: UUID, blob_id: UUID) -> BlobRecord | None: ...


class TerminalBlobUnitOfWork(Protocol):
    blob_access: TerminalBlobAccessRepository

    def __enter__(self) -> TerminalBlobUnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
