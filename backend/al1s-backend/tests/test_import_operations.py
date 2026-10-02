import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, Response

from al1s.api.maa_import_operations import operation, submit
from al1s.maa.types import ImportStatus


def batch():
    return SimpleNamespace(
        batch_id=uuid4(), created_at=datetime.now(UTC), status=ImportStatus.PROCESSING,
        row_version=1, error_code=None,
    )


def test_async_submit_returns_persisted_identity_before_work_and_holds_slot():
    async def run():
        service = Mock()
        value = batch()
        service.prepare_upload.return_value = (object(), value, False)
        slots = asyncio.Semaphore(1)
        async def stream():
            yield b"zip"
        request = SimpleNamespace(
            headers={"content-type": "application/zip"}, stream=stream,
            app=SimpleNamespace(state=SimpleNamespace(
                maa_archive_upload_slots=slots, maa_archive_import_commands=service,
            )),
        )
        background, response = BackgroundTasks(), Response()
        result = await submit(request, background, response)
        assert result["operation_id"] == str(value.batch_id)
        assert response.headers["Location"].startswith("/api/v1/maa/imports/")
        service.finish_upload.assert_not_called()
        assert slots.locked()
        await background()
        service.finish_upload.assert_called_once()
        assert not slots.locked()
    asyncio.run(run())


def test_failed_prepare_releases_capacity():
    async def run():
        service = Mock()
        service.prepare_upload.side_effect = ValueError("invalid")
        slots = asyncio.Semaphore(1)
        async def stream():
            yield b"invalid"
        request = SimpleNamespace(
            headers={"content-type": "application/zip"}, stream=stream,
            app=SimpleNamespace(state=SimpleNamespace(
                maa_archive_upload_slots=slots, maa_archive_import_commands=service,
            )),
        )
        with pytest.raises(ValueError):
            await submit(request, BackgroundTasks(), Response())
        assert not slots.locked()
    asyncio.run(run())


def test_deadline_unknown_and_cancelled_are_not_success():
    value = batch()
    value.created_at -= timedelta(minutes=30)
    assert operation(value)["state"] == "result_unknown"
    value.status = ImportStatus.FAILED
    value.error_code = "archive_import_cancelled"
    assert operation(value)["state"] == "cancelled"
    assert not operation(value)["can_cancel"]
