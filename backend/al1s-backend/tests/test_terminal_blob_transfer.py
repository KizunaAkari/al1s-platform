from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import TracebackType
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from al1s.api.terminal_blobs import router
from al1s.execution.blob_transfer import (
    MAX_BLOB_RANGE_BYTES,
    TerminalBlobTransferService,
)
from al1s.execution.errors import InvalidRequestError, NotFoundError
from al1s.kernel.types import BlobRecord, BlobStatus


class FakeBlobAccess:
    def __init__(self, terminal_id: UUID, blob: BlobRecord) -> None:
        self._terminal_id = terminal_id
        self._blob = blob

    def find_authorized(self, terminal_id: UUID, blob_id: UUID) -> BlobRecord | None:
        if terminal_id == self._terminal_id and blob_id == self._blob.blob_id:
            return self._blob
        return None


class FakeUnitOfWork:
    def __init__(self, access: FakeBlobAccess) -> None:
        self.blob_access = access

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeBlobStore:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.ranges: list[tuple[str, int, int]] = []

    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes:
        self.ranges.append((object_key, start, end_inclusive))
        return self.body[start : end_inclusive + 1]


@dataclass(frozen=True)
class AuthenticatedTerminal:
    terminal_id: UUID


class FakeResources:
    def __init__(self, terminal_id: UUID) -> None:
        self._terminal_id = terminal_id

    def authenticate_terminal(self, credential: str) -> AuthenticatedTerminal:
        assert credential == "credential"
        return AuthenticatedTerminal(self._terminal_id)


def _fixture() -> tuple[UUID, BlobRecord, FakeBlobStore, TerminalBlobTransferService]:
    terminal_id = uuid4()
    body = b"0123456789"
    blob = BlobRecord(
        blob_id=uuid4(),
        sha256="a" * 64,
        size_bytes=len(body),
        media_type="application/octet-stream",
        object_key="sha256/aa/resource",
        status=BlobStatus.READY,
        row_version=1,
        created_at=datetime(2026, 8, 31, tzinfo=UTC),
        ready_at=datetime(2026, 8, 31, tzinfo=UTC),
        deleted_at=None,
    )
    store = FakeBlobStore(body)
    access = FakeBlobAccess(terminal_id, blob)
    return (
        terminal_id,
        blob,
        store,
        TerminalBlobTransferService(lambda: FakeUnitOfWork(access), store),
    )


def test_service_requires_assignment_and_bounds_each_range() -> None:
    terminal_id, blob, store, service = _fixture()

    metadata, body = service.read_range(terminal_id, blob.blob_id, start=2, end_inclusive=5)

    assert body == b"2345"
    assert metadata.sha256 == "a" * 64
    assert store.ranges == [(blob.object_key, 2, 5)]
    with pytest.raises(NotFoundError):
        service.metadata(uuid4(), blob.blob_id)
    large_blob = replace(blob, size_bytes=MAX_BLOB_RANGE_BYTES + 1)
    large_service = TerminalBlobTransferService(
        lambda: FakeUnitOfWork(FakeBlobAccess(terminal_id, large_blob)), store
    )
    with pytest.raises(InvalidRequestError, match="must not exceed"):
        large_service.read_range(
            terminal_id,
            blob.blob_id,
            start=0,
            end_inclusive=MAX_BLOB_RANGE_BYTES,
        )


def test_http_head_and_range_contract() -> None:
    terminal_id, blob, _store, service = _fixture()
    app = FastAPI()
    app.state.terminal_blob_transfer = service
    app.state.execution_resources = FakeResources(terminal_id)
    app.include_router(router, prefix="/api/v1/terminal")

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            head = await client.head(
                f"/api/v1/terminal/blobs/{blob.blob_id}",
                headers={"Authorization": "Bearer credential"},
            )
            partial = await client.get(
                f"/api/v1/terminal/blobs/{blob.blob_id}",
                headers={"Authorization": "Bearer credential", "Range": "bytes=3-6"},
            )
            return head, partial

    head, partial = asyncio.run(exercise())

    assert head.status_code == 200
    assert head.headers["content-length"] == "10"
    assert head.headers["x-content-sha256"] == "a" * 64
    assert partial.status_code == 206
    assert partial.content == b"3456"
    assert partial.headers["content-range"] == "bytes 3-6/10"
