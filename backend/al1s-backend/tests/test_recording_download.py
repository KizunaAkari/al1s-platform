from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from al1s.adapters.postgres.recording_reader import downloadable_recording_query
from al1s.api.task_recordings import router
from al1s.execution.errors import ExecutionDomainError, InvalidRequestError, NotFoundError
from al1s.execution.recording_download import (
    MAX_RECORDING_RANGE,
    RecordingDownloadService,
    RecordingFile,
    RecordingPage,
    RecordingSummary,
)


class Reader:
    def __init__(self):
        self.task, self.artifact = uuid4(), uuid4()
        self.calls = 0
        self.allowed = True
        self.item = RecordingFile("测试\r\n/video.mp4", "a" * 64, 8, "private/object-key")

    def find_downloadable(self, task, artifact, now):
        self.calls += 1
        if self.allowed and (task, artifact) == (self.task, self.artifact):
            return self.item
        return None

    def list_recordings(self, task, now, cursor, limit):
        if not self.allowed or task != self.task or cursor == self.artifact:
            return RecordingPage((), None)
        return RecordingPage(
            (RecordingSummary(self.artifact, uuid4(), self.item.file_name, "ready", True),), None
        )


class Store:
    def __init__(self):
        self.calls = 0
        self.body = b"abcdefgh"

    def get_range(self, key, start, end):
        self.calls += 1
        assert key == "private/object-key"
        return self.body[start : end + 1]


def test_range_is_original_bounded_and_reauthorized():
    reader, store = Reader(), Store()
    service = RecordingDownloadService(reader, store)
    _, body = service.read_range(reader.task, reader.artifact, 2, 5)
    assert body == b"cdef"
    assert reader.calls == store.calls == 1
    reader.allowed = False
    with pytest.raises(NotFoundError):
        service.read_range(reader.task, reader.artifact, 0, 1)
    assert store.calls == 1


@pytest.mark.parametrize("start,end", [(-1, 1), (4, 3), (0, 8)])
def test_invalid_range_does_not_read_store(start, end):
    reader, store = Reader(), Store()
    with pytest.raises(InvalidRequestError):
        RecordingDownloadService(reader, store).read_range(reader.task, reader.artifact, start, end)
    assert store.calls == 0


def test_large_range_and_wrong_task_are_rejected():
    reader, store = Reader(), Store()
    reader.item = RecordingFile("v.mp4", "a" * 64, MAX_RECORDING_RANGE + 1, "private/object-key")
    service = RecordingDownloadService(reader, store)
    with pytest.raises(InvalidRequestError):
        service.read_range(reader.task, reader.artifact, 0, MAX_RECORDING_RANGE)
    with pytest.raises(NotFoundError):
        service.metadata(uuid4(), reader.artifact)
    assert store.calls == 0


def test_truncated_store_range_rejected():
    reader, store = Reader(), Store()
    store.body = b"abc"
    with pytest.raises(RuntimeError, match="length"):
        RecordingDownloadService(reader, store).read_range(reader.task, reader.artifact, 0, 7)


def test_http_headers_range_and_credential_boundary():
    reader, store = Reader(), Store()
    app = FastAPI()
    app.state.recording_downloads = RecordingDownloadService(reader, store)
    app.include_router(router, prefix="/tasks")

    @app.exception_handler(ExecutionDomainError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    with TestClient(app) as client:
        url = f"/tasks/{reader.task}/recordings/{reader.artifact}"
        head = client.head(url)
        assert head.status_code == 200 and not head.content
        assert head.headers["content-length"] == "8"
        assert "private/object-key" not in str(head.headers)
        assert "%E6%B5%8B" in head.headers["content-disposition"]
        assert "\r" not in head.headers["content-disposition"]
        assert client.get(url).status_code == 400
        assert client.get(url, headers={"Range": "bytes=0-" + "9" * 4500}).status_code == 400
        assert client.get(url, headers={"Range": "bytes=1-3,5-6"}).status_code == 400
        result = client.get(url, headers={"Range": "bytes=1-3"})
        assert result.status_code == 206 and result.content == b"bcd"
        assert result.headers["content-range"] == "bytes 1-3/8"
        assert result.headers["cache-control"] == "no-store"
        assert client.head(url, headers={"Authorization": "Bearer terminal"}).status_code == 403
        full = client.get(url + "/download")
        assert full.status_code == 200 and full.content == store.body
        assert full.headers["content-length"] == "8"
        listing_url = f"/tasks/{reader.task}/recordings"
        listing = client.get(listing_url)
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 1
        assert "object_key" not in listing.text
        assert client.get(listing_url, params={"limit": 51}).status_code == 422
        assert client.get(listing_url, params={"cursor": "bad"}).status_code == 422
        assert (
            client.get(listing_url, params={"cursor": str(reader.artifact)}).json()["items"] == []
        )
        assert (
            client.get(url + "/download", headers={"Authorization": "Bearer terminal"}).status_code
            == 403
        )


def test_stream_stops_after_access_revoked():
    reader, store = Reader(), Store()
    reader.item = RecordingFile("v.mp4", "a" * 64, MAX_RECORDING_RANGE + 1, "private/object-key")
    store.body = b"x" * (MAX_RECORDING_RANGE + 1)
    service = RecordingDownloadService(reader, store)
    stream = service.stream(reader.task, reader.artifact, reader.item)
    assert len(next(stream)) == MAX_RECORDING_RANGE
    reader.allowed = False
    with pytest.raises(NotFoundError):
        next(stream)
    assert store.calls == 1


def test_query_is_parameterized_bounded_and_has_retention_and_ownership_filters():
    now = datetime(2026, 9, 8, tzinfo=UTC)
    compiled = downloadable_recording_query(uuid4(), uuid4(), now).compile(
        dialect=postgresql.dialect()
    )
    sql = str(compiled)
    for required in [
        "task_requests.deleted_at IS NULL",
        "record_video IS true",
        "execution_attempts.ended_at >",
        "terminal_artifacts.owner_kind =",
        "blob_objects.status =",
        "LIMIT",
    ]:
        assert required in sql
    assert now not in compiled.params.values()
    assert (
        now - next(value for value in compiled.params.values() if isinstance(value, datetime))
    ).days == 30
